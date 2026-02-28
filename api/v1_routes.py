"""Versioned v1 API surface for fraud-check resources."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from flask import Blueprint, Response, current_app, jsonify, request
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import BadRequest

from models import Customer, FraudCheck, IdempotencyRecord, db
from services.fraud_engine import categorize_merchant, score_transaction
from services.gemini_service import GeminiServiceError
from services.nessie_service import NessieServiceError

v1_bp = Blueprint("v1_api", __name__, url_prefix="/v1")


def _error(error_type: str, message: str, status: int) -> tuple[Response, int]:
    """Return consistent API error envelope."""
    return jsonify({"error": {"type": error_type, "message": message}}), status


def _parse_json_body() -> dict:
    """Parse JSON body while distinguishing malformed payloads."""
    try:
        payload = request.get_json(silent=False)
    except BadRequest as exc:
        raise ValueError("malformed_json") from exc
    if payload is None:
        raise ValueError("empty_json")
    if not isinstance(payload, dict):
        raise ValueError("invalid_json_type")
    return payload


def _parse_iso_timestamp(value: str) -> datetime:
    """Parse ISO-8601 timestamp including trailing Z."""
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _request_hash(payload: dict) -> str:
    """Hash payload for idempotency matching."""
    encoded = json.dumps(payload or {}, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _load_idempotent_response(scope_endpoint: str, payload: dict) -> Response | None:
    """Return cached response for matching idempotency key."""
    key = request.headers.get("Idempotency-Key")
    if not key:
        return None

    record = IdempotencyRecord.query.filter_by(
        idempotency_key=key,
        method=request.method,
        endpoint=scope_endpoint,
    ).first()
    if not record:
        return None

    body_hash = _request_hash(payload)
    if record.request_hash != body_hash:
        return _error(
            "idempotency_mismatch",
            "Idempotency-Key was already used with a different request body.",
            409,
        )
    return jsonify(json.loads(record.response_body)), record.response_status


def _store_idempotent_response(
    scope_endpoint: str,
    payload: dict,
    response_body: dict,
    status_code: int,
) -> None:
    """Persist response for idempotent retries."""
    key = request.headers.get("Idempotency-Key")
    if not key:
        return

    db.session.add(
        IdempotencyRecord(
            idempotency_key=key,
            method=request.method,
            endpoint=scope_endpoint,
            request_hash=_request_hash(payload),
            response_status=status_code,
            response_body=json.dumps(response_body),
        )
    )
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()


def _history_for_customer(nessie_customer_id: str) -> list[dict]:
    """Build Nessie-only history baseline for fraud scoring."""
    nessie = current_app.extensions["nessie_service"]
    nessie_history: list[dict] = []
    try:
        nessie_history = nessie.get_customer_history(nessie_customer_id)
    except NessieServiceError:
        nessie_history = []
    nessie_history.sort(key=lambda item: item.get("timestamp", ""))
    return nessie_history


@v1_bp.route("/fraud-checks", methods=["POST"])
def create_fraud_check() -> tuple[Response, int] | Response:
    """
    Create a fraud-check resource.
    ---
    tags:
      - v1-fraud-checks
    consumes:
      - application/json
    produces:
      - application/json
    parameters:
      - in: header
        name: Idempotency-Key
        type: string
        required: false
      - in: body
        name: body
        required: false
        description: Fraud check payload as JSON object.
        schema:
          type: object
          properties:
            customer_id:
              type: string
            amount:
              type: number
            merchant:
              type: string
            location:
              type: string
            timestamp:
              type: string
              format: date-time
          example:
            customer_id: "69a29b7e95150878eaffa4ea"
            amount: 1200
            merchant: "Best Buy"
            location: "Chicago"
            timestamp: "2026-06-01T02:10:00Z"
    responses:
      201:
        description: Fraud check created
      400:
        description: Malformed JSON
      422:
        description: Validation error
      404:
        description: Customer not found
      409:
        description: Idempotency conflict
      502:
        description: Upstream dependency failure
    """
    try:
        payload = _parse_json_body()
    except ValueError as exc:
        if str(exc) == "malformed_json":
            return _error("malformed_json", "Request body must contain valid JSON.", 400)
        return _error(
            "invalid_request",
            "Request body must be a JSON object.",
            400,
        )

    idem = _load_idempotent_response("/v1/fraud-checks", payload)
    if idem:
        return idem

    customer_id = (payload.get("customer_id") or "").strip()
    merchant = (payload.get("merchant") or "").strip()
    location = (payload.get("location") or "").strip()
    timestamp_raw = payload.get("timestamp")
    amount_raw = payload.get("amount")

    missing_fields = []
    if not customer_id:
        missing_fields.append("customer_id")
    if not merchant:
        missing_fields.append("merchant")
    if not location:
        missing_fields.append("location")
    if not timestamp_raw:
        missing_fields.append("timestamp")
    if missing_fields:
        return _error(
            "invalid_request",
            f"Missing required fields: {', '.join(missing_fields)}.",
            422,
        )

    try:
        amount = float(amount_raw)
    except (TypeError, ValueError):
        return _error("invalid_request", "amount must be numeric.", 422)
    if amount <= 0:
        return _error("invalid_request", "amount must be greater than zero.", 422)

    try:
        timestamp = _parse_iso_timestamp(timestamp_raw)
    except (TypeError, ValueError):
        return _error("invalid_request", "timestamp must be a valid ISO-8601 string.", 422)

    nessie = current_app.extensions["nessie_service"]
    nessie_customer_id = customer_id
    local_customer = Customer.query.get(customer_id)
    if local_customer and local_customer.nessie_customer_id:
        nessie_customer_id = local_customer.nessie_customer_id
    try:
        remote_customer = nessie.get_customer(nessie_customer_id)
    except NessieServiceError as exc:
        return _error("upstream_error", str(exc), 502)
    if not remote_customer:
        return _error("not_found", "customer not found in Nessie.", 404)

    fraud_check = FraudCheck(
        customer_id=nessie_customer_id,
        amount=amount,
        merchant=merchant,
        merchant_category=categorize_merchant(merchant),
        location=location,
        timestamp=timestamp,
        status="processing",
        review_status="open",
    )
    db.session.add(fraud_check)
    db.session.flush()

    history = _history_for_customer(nessie_customer_id)
    analysis = score_transaction(
        amount=amount,
        location=location,
        timestamp=timestamp,
        merchant_category=fraud_check.merchant_category,
        history=history,
    )

    gemini = current_app.extensions["gemini_service"]
    explain_payload = {
        "fraud_check_id": fraud_check.id,
        "customer": remote_customer,
        "transaction": {
            "amount": amount,
            "merchant": merchant,
            "location": location,
            "timestamp": timestamp.isoformat(),
            "merchant_category": fraud_check.merchant_category,
        },
        "history_sample_size": len(history),
        "rule_factors": analysis.debug_factors,
    }
    try:
        ai_explanation = gemini.generate_explanation(explain_payload)
    except GeminiServiceError:
        ai_explanation = (
            "AI explanation is temporarily unavailable. "
            "Use risk_factors for deterministic explanation."
        )

    fraud_check.status = "completed"
    fraud_check.fraud_score = analysis.fraud_score
    fraud_check.risk_level = analysis.risk_level
    fraud_check.risk_factors = analysis.risk_factors
    fraud_check.ai_explanation = ai_explanation
    db.session.commit()

    response_body = {"fraud_check": fraud_check.to_dict()}
    _store_idempotent_response("/v1/fraud-checks", payload, response_body, 201)
    return jsonify(response_body), 201


@v1_bp.route("/fraud-checks/<fraud_check_id>", methods=["GET"])
def get_fraud_check(fraud_check_id: str) -> tuple[Response, int]:
    """
    Retrieve a fraud-check resource by id.
    ---
    tags:
      - v1-fraud-checks
    parameters:
      - in: path
        name: fraud_check_id
        required: true
        type: string
        default: latest
        default: latest
    responses:
      200:
        description: Fraud check found
      404:
        description: Fraud check not found
    """
    if fraud_check_id == "latest":
        fraud_check = FraudCheck.query.order_by(FraudCheck.created_at.desc()).first()
    else:
        fraud_check = FraudCheck.query.get(fraud_check_id)
    if not fraud_check:
        return _error("not_found", "fraud_check not found.", 404)
    return jsonify({"fraud_check": fraud_check.to_dict()}), 200


@v1_bp.route("/fraud-checks", methods=["GET"])
def list_fraud_checks() -> tuple[Response, int]:
    """
    List fraud-check resources with filters and pagination.
    ---
    tags:
      - v1-fraud-checks
    parameters:
      - in: query
        name: page
        type: integer
        default: 1
      - in: query
        name: per_page
        type: integer
        default: 10
      - in: query
        name: customer_id
        type: string
      - in: query
        name: status
        type: string
      - in: query
        name: risk_level
        type: string
      - in: query
        name: min_fraud_score
        type: number
      - in: query
        name: max_fraud_score
        type: number
      - in: query
        name: review_status
        type: string
      - in: query
        name: q
        type: string
    responses:
      200:
        description: Paginated fraud checks
      422:
        description: Invalid pagination input
    """
    page = request.args.get("page", default=1, type=int)
    per_page = request.args.get("per_page", default=10, type=int)
    if page < 1:
        return _error("invalid_request", "page must be >= 1.", 422)
    if per_page < 1 or per_page > 100:
        return _error("invalid_request", "per_page must be between 1 and 100.", 422)

    query = FraudCheck.query.order_by(FraudCheck.created_at.desc())

    customer_id = request.args.get("customer_id", type=str)
    status = request.args.get("status", type=str)
    risk_level = request.args.get("risk_level", type=str)
    review_status = request.args.get("review_status", type=str)
    min_score = request.args.get("min_fraud_score", type=float)
    max_score = request.args.get("max_fraud_score", type=float)
    q = request.args.get("q", type=str)

    if customer_id:
        query = query.filter(FraudCheck.customer_id == customer_id)
    if status:
        query = query.filter(FraudCheck.status == status)
    if risk_level:
        query = query.filter(FraudCheck.risk_level == risk_level.upper())
    if review_status:
        query = query.filter(FraudCheck.review_status == review_status.lower())
    if min_score is not None and not (0 <= min_score <= 1):
        return _error("invalid_request", "min_fraud_score must be between 0 and 1.", 422)
    if max_score is not None and not (0 <= max_score <= 1):
        return _error("invalid_request", "max_fraud_score must be between 0 and 1.", 422)
    if min_score is not None and max_score is not None and min_score > max_score:
        return _error(
            "invalid_request",
            "min_fraud_score cannot be greater than max_fraud_score.",
            422,
        )
    if min_score is not None:
        query = query.filter(FraudCheck.fraud_score >= min_score)
    if max_score is not None:
        query = query.filter(FraudCheck.fraud_score <= max_score)
    if q:
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(
                FraudCheck.customer_id.ilike(pattern),
                FraudCheck.merchant.ilike(pattern),
                FraudCheck.location.ilike(pattern),
                FraudCheck.risk_level.ilike(pattern),
                FraudCheck.review_status.ilike(pattern),
            )
        )

    paginated = query.paginate(page=page, per_page=per_page, error_out=False)
    return (
        jsonify(
            {
                "items": [item.to_dict() for item in paginated.items],
                "pagination": {
                    "page": paginated.page,
                    "per_page": paginated.per_page,
                    "pages": paginated.pages,
                    "total": paginated.total,
                },
            }
        ),
        200,
    )


@v1_bp.route("/health", methods=["GET"])
def health() -> tuple[Response, int]:
    """
    Health probe endpoint.
    ---
    tags:
      - v1-system
    produces:
      - application/json
    responses:
      200:
        description: Service healthy
    """
    return jsonify({"status": "ok", "service": "fraudlens-api"}), 200


@v1_bp.route("/fraud-checks/<fraud_check_id>", methods=["PATCH"])
def patch_fraud_check(fraud_check_id: str) -> tuple[Response, int]:
    """
    Update mutable review state for a fraud-check resource.
    ---
    tags:
      - v1-fraud-checks
    consumes:
      - application/json
    produces:
      - application/json
    parameters:
      - in: path
        name: fraud_check_id
        required: true
        type: string
        default: latest
      - in: body
        name: body
        required: false
        description: Update payload as JSON object.
        schema:
          type: object
          properties:
            review_status:
              type: string
              enum: [open, confirmed_fraud, dismissed]
          example:
            review_status: dismissed
    responses:
      200:
        description: Fraud check updated
      404:
        description: Fraud check not found
      422:
        description: Invalid review status
    """
    if fraud_check_id == "latest":
        fraud_check = FraudCheck.query.order_by(FraudCheck.created_at.desc()).first()
    else:
        fraud_check = FraudCheck.query.get(fraud_check_id)
    if not fraud_check:
        return _error("not_found", "fraud_check not found.", 404)

    try:
        payload = _parse_json_body()
    except ValueError as exc:
        if str(exc) == "malformed_json":
            return _error("malformed_json", "Request body must contain valid JSON.", 400)
        return _error("invalid_request", "Request body must be a JSON object.", 400)

    review_status = (payload.get("review_status") or "").strip().lower()
    allowed = {"open", "confirmed_fraud", "dismissed"}
    if review_status not in allowed:
        return _error(
            "invalid_request",
            "review_status must be one of: open, confirmed_fraud, dismissed.",
            422,
        )

    fraud_check.review_status = review_status
    db.session.commit()
    return jsonify({"fraud_check": fraud_check.to_dict()}), 200


@v1_bp.route("/fraud-checks/<fraud_check_id>", methods=["DELETE"])
def delete_fraud_check(fraud_check_id: str) -> tuple[Response, int]:
    """
    Delete a fraud-check resource.
    ---
    tags:
      - v1-fraud-checks
    parameters:
      - in: path
        name: fraud_check_id
        required: true
        type: string
    responses:
      200:
        description: Deleted
      404:
        description: Fraud check not found
    """
    if fraud_check_id == "latest":
        fraud_check = FraudCheck.query.order_by(FraudCheck.created_at.desc()).first()
    else:
        fraud_check = FraudCheck.query.get(fraud_check_id)
    if not fraud_check:
        return _error("not_found", "fraud_check not found.", 404)
    deleted_id = fraud_check.id
    db.session.delete(fraud_check)
    db.session.commit()
    return jsonify({"deleted": True, "fraud_check_id": deleted_id}), 200

