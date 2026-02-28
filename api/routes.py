"""REST API blueprint for FraudLens."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from flask import Blueprint, Response, current_app, jsonify, request
from sqlalchemy.exc import IntegrityError

from models import Customer, IdempotencyRecord, Transaction, db
from services.fraud_engine import categorize_merchant, score_transaction
from services.gemini_service import GeminiServiceError
from services.nessie_service import NessieServiceError

api_bp = Blueprint("api", __name__, url_prefix="/api")


def _json_error(message: str, status: int) -> tuple[Response, int]:
    """Build a standardized JSON error response."""
    return jsonify({"error": message}), status


def _parse_iso_timestamp(value: str) -> datetime:
    """Parse ISO-8601 timestamps including trailing Z."""
    if not value:
        raise ValueError("timestamp is required")
    normalized = value.replace("Z", "+00:00")
    timestamp = datetime.fromisoformat(normalized)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _request_hash(payload: dict) -> str:
    """Build a deterministic hash for idempotent POST requests."""
    encoded = json.dumps(payload or {}, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _get_idempotent_response(scope_endpoint: str, payload: dict) -> Response | None:
    """Return stored response for matching idempotency key, if available."""
    key = request.headers.get("Idempotency-Key")
    if not key:
        return None

    body_hash = _request_hash(payload)
    record = IdempotencyRecord.query.filter_by(
        idempotency_key=key,
        method=request.method,
        endpoint=scope_endpoint,
    ).first()
    if not record:
        return None

    if record.request_hash != body_hash:
        response, status = _json_error(
            "Idempotency-Key already used with a different request payload.",
            409,
        )
        response.status_code = status
        return response

    stored_payload = json.loads(record.response_body)
    return jsonify(stored_payload), record.response_status


def _store_idempotent_response(
    scope_endpoint: str,
    payload: dict,
    response_body: dict,
    status_code: int,
) -> None:
    """Persist idempotent response payload for future retries."""
    key = request.headers.get("Idempotency-Key")
    if not key:
        return

    record = IdempotencyRecord(
        idempotency_key=key,
        method=request.method,
        endpoint=scope_endpoint,
        request_hash=_request_hash(payload),
        response_status=status_code,
        response_body=json.dumps(response_body),
    )
    db.session.add(record)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()


def _transaction_history_for_customer(customer_id: str, exclude_id: str | None = None) -> list:
    """Return transaction history dictionaries sorted by timestamp."""
    query = Transaction.query.filter_by(customer_id=customer_id).order_by(Transaction.timestamp)
    if exclude_id:
        query = query.filter(Transaction.id != exclude_id)
    return [
        {
            "id": txn.id,
            "amount": txn.amount,
            "location": txn.location,
            "merchant_category": txn.merchant_category,
            "timestamp": txn.timestamp.isoformat(),
        }
        for txn in query.all()
    ]


def _combined_customer_history(
    customer: Customer, exclude_local_id: str | None = None
) -> tuple[list[dict], bool]:
    """
    Return local+Nessie history for fraud baselining.

    Returns:
      - merged/sorted history
      - whether Nessie was successfully queried
    """
    local_history = _transaction_history_for_customer(
        customer_id=customer.id,
        exclude_id=exclude_local_id,
    )
    for item in local_history:
        item["source"] = "local"

    remote_history: list[dict] = []
    nessie_ok = False
    nessie = current_app.extensions["nessie_service"]
    if customer.nessie_customer_id:
        try:
            remote_history = nessie.get_customer_history(customer.nessie_customer_id)
            for item in remote_history:
                if item.get("merchant"):
                    item["merchant_category"] = categorize_merchant(item["merchant"])
            nessie_ok = True
        except NessieServiceError:
            nessie_ok = False

    merged = local_history + remote_history
    merged.sort(key=lambda item: item.get("timestamp", ""))
    return merged, nessie_ok


@api_bp.route("/customers", methods=["POST"])
def create_customer() -> tuple[Response, int] | Response:
    """Create customer in Nessie and local SQLite database."""
    payload = request.get_json(silent=True) or {}
    idem = _get_idempotent_response("/api/customers", payload)
    if idem:
        return idem

    first_name = (payload.get("first_name") or "").strip()
    last_name = (payload.get("last_name") or "").strip()
    if not first_name or not last_name:
        return _json_error("first_name and last_name are required.", 400)

    nessie = current_app.extensions["nessie_service"]
    try:
        remote_customer = nessie.create_customer(first_name=first_name, last_name=last_name)
    except NessieServiceError as exc:
        return _json_error(str(exc), 502)

    customer = Customer(
        first_name=first_name,
        last_name=last_name,
        nessie_customer_id=remote_customer.customer_id,
    )
    db.session.add(customer)
    db.session.commit()

    response_body = {"customer": customer.to_dict()}
    _store_idempotent_response("/api/customers", payload, response_body, 201)
    return jsonify(response_body), 201


@api_bp.route("/customers/remote", methods=["GET"])
def list_remote_customers() -> tuple[Response, int]:
    """
    Return customers directly from Nessie without local persistence.
    ---
    tags:
      - api-customers
    parameters:
      - in: query
        name: limit
        type: integer
        default: 100
      - in: query
        name: offset
        type: integer
        default: 0
      - in: query
        name: newest_first
        type: boolean
        default: false
    responses:
      200:
        description: Nessie customers fetched
      400:
        description: Invalid query parameters
      502:
        description: Nessie upstream failure
    """
    limit = request.args.get("limit", default=100, type=int)
    offset = request.args.get("offset", default=0, type=int)
    newest_first = request.args.get("newest_first", default="false", type=str).lower()
    if limit < 1 or limit > 500:
        return _json_error("limit must be between 1 and 500.", 400)
    if offset < 0:
        return _json_error("offset must be >= 0.", 400)
    if newest_first not in {"true", "false"}:
        return _json_error("newest_first must be true or false.", 400)

    nessie = current_app.extensions["nessie_service"]
    try:
        # Fetch a larger window first so offset/newest-first can be applied predictably.
        remote_customers = nessie.list_customers(limit=500)
    except NessieServiceError as exc:
        return _json_error(str(exc), 502)

    if newest_first == "true":
        remote_customers = list(reversed(remote_customers))

    total = len(remote_customers)
    items = remote_customers[offset : offset + limit]
    return (
        jsonify(
            {
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset,
                "newest_first": newest_first == "true",
                "has_more": (offset + limit) < total,
            }
        ),
        200,
    )


@api_bp.route("/customers/sync", methods=["POST"])
def sync_remote_customers() -> tuple[Response, int]:
    """
    Pull customers from Nessie and upsert them into local SQLite storage.

    Matching strategy:
      1) existing `nessie_customer_id`
      2) fallback name pair for older local rows without Nessie linkage
    """
    payload = request.get_json(silent=True) or {}
    idem = _get_idempotent_response("/api/customers/sync", payload)
    if idem:
        return idem

    limit = payload.get("limit", 100)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return _json_error("limit must be an integer.", 400)
    if limit < 1 or limit > 500:
        return _json_error("limit must be between 1 and 500.", 400)

    nessie = current_app.extensions["nessie_service"]
    try:
        remote_customers = nessie.list_customers(limit=limit)
    except NessieServiceError as exc:
        return _json_error(str(exc), 502)

    created = 0
    updated = 0
    unchanged = 0
    synced_items: list[dict] = []

    for item in remote_customers:
        nessie_customer_id = item["nessie_customer_id"]
        first_name = (item.get("first_name") or "").strip()
        last_name = (item.get("last_name") or "").strip()
        if not first_name or not last_name:
            continue

        customer = Customer.query.filter_by(nessie_customer_id=nessie_customer_id).first()
        if not customer:
            customer = Customer.query.filter_by(
                first_name=first_name,
                last_name=last_name,
            ).first()

        if not customer:
            customer = Customer(
                first_name=first_name,
                last_name=last_name,
                nessie_customer_id=nessie_customer_id,
            )
            db.session.add(customer)
            db.session.flush()
            created += 1
        else:
            changed = False
            if customer.nessie_customer_id != nessie_customer_id:
                customer.nessie_customer_id = nessie_customer_id
                changed = True
            if customer.first_name != first_name:
                customer.first_name = first_name
                changed = True
            if customer.last_name != last_name:
                customer.last_name = last_name
                changed = True
            if changed:
                updated += 1
            else:
                unchanged += 1

        synced_items.append(customer.to_dict())

    db.session.commit()
    response_body = {
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "synced_count": len(synced_items),
        "items": synced_items,
    }
    _store_idempotent_response("/api/customers/sync", payload, response_body, 200)
    return jsonify(response_body), 200


@api_bp.route("/transactions", methods=["POST"])
def create_transaction() -> tuple[Response, int] | Response:
    """Create and score a transaction, then persist it."""
    payload = request.get_json(silent=True) or {}
    idem = _get_idempotent_response("/api/transactions", payload)
    if idem:
        return idem

    customer_id = (payload.get("customer_id") or "").strip()
    merchant = (payload.get("merchant") or "").strip()
    location = (payload.get("location") or "").strip()
    amount = payload.get("amount")
    timestamp_raw = payload.get("timestamp")

    if not customer_id or not merchant or not location:
        return _json_error("customer_id, merchant, and location are required.", 400)

    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return _json_error("amount must be a valid number.", 400)
    if amount <= 0:
        return _json_error("amount must be greater than zero.", 400)

    try:
        timestamp = _parse_iso_timestamp(timestamp_raw)
    except (TypeError, ValueError):
        return _json_error("timestamp must be a valid ISO-8601 string.", 400)

    customer = Customer.query.get(customer_id)
    if not customer:
        return _json_error("customer not found.", 404)

    merchant_category = categorize_merchant(merchant)
    history, nessie_history_available = _combined_customer_history(customer)
    analysis = score_transaction(
        amount=amount,
        location=location,
        timestamp=timestamp,
        merchant_category=merchant_category,
        history=history,
    )

    transaction = Transaction(
        customer_id=customer_id,
        amount=amount,
        merchant=merchant,
        merchant_category=merchant_category,
        location=location,
        timestamp=timestamp,
        fraud_score=analysis.fraud_score,
        risk_level=analysis.risk_level,
    )
    transaction.risk_factors = analysis.risk_factors
    db.session.add(transaction)
    db.session.commit()

    response_body = {
        "transaction_id": transaction.id,
        "fraud_score": analysis.fraud_score,
        "risk_level": analysis.risk_level,
        "history_sample_size": len(history),
        "nessie_history_available": nessie_history_available,
    }
    _store_idempotent_response("/api/transactions", payload, response_body, 201)
    return jsonify(response_body), 201


@api_bp.route("/transactions", methods=["GET"])
def list_transactions() -> tuple[Response, int]:
    """List transactions with pagination support."""
    page = request.args.get("page", default=1, type=int)
    per_page = request.args.get(
        "per_page",
        default=current_app.config["DEFAULT_PAGE_SIZE"],
        type=int,
    )
    customer_id = request.args.get("customer_id", type=str)

    if page < 1:
        return _json_error("page must be >= 1.", 400)
    if per_page < 1 or per_page > current_app.config["MAX_PAGE_SIZE"]:
        return _json_error(
            f"per_page must be between 1 and {current_app.config['MAX_PAGE_SIZE']}.",
            400,
        )

    query = Transaction.query.order_by(Transaction.timestamp.desc())
    if customer_id:
        query = query.filter_by(customer_id=customer_id)

    paginated = query.paginate(page=page, per_page=per_page, error_out=False)
    return (
        jsonify(
            {
                "items": [txn.to_dict() for txn in paginated.items],
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


@api_bp.route("/fraud-score/<transaction_id>", methods=["GET"])
def get_fraud_score(transaction_id: str) -> tuple[Response, int]:
    """Recompute fraud score and enrich output with Gemini explanation."""
    transaction = Transaction.query.get(transaction_id)
    if not transaction:
        return _json_error("transaction not found.", 404)

    customer = Customer.query.get(transaction.customer_id)
    if not customer:
        return _json_error("customer not found for this transaction.", 404)

    history, nessie_history_available = _combined_customer_history(
        customer=customer,
        exclude_local_id=transaction.id,
    )
    analysis = score_transaction(
        amount=transaction.amount,
        location=transaction.location,
        timestamp=transaction.timestamp,
        merchant_category=transaction.merchant_category,
        history=history,
    )

    transaction.fraud_score = analysis.fraud_score
    transaction.risk_level = analysis.risk_level
    transaction.risk_factors = analysis.risk_factors
    db.session.commit()

    gemini = current_app.extensions["gemini_service"]
    explanation_payload = {
        "transaction": transaction.to_dict(),
        "customer": customer.to_dict(),
        "history_sample_size": len(history),
        "nessie_history_available": nessie_history_available,
        "rule_factors": analysis.debug_factors,
    }
    try:
        ai_explanation = gemini.generate_explanation(explanation_payload)
    except GeminiServiceError:
        ai_explanation = (
            "AI explanation is temporarily unavailable. "
            "Rule-based risk factors are still provided."
        )

    return (
        jsonify(
            {
                "transaction_id": transaction.id,
                "fraud_score": analysis.fraud_score,
                "risk_level": analysis.risk_level,
                "risk_factors": analysis.risk_factors,
                "history_sample_size": len(history),
                "nessie_history_available": nessie_history_available,
                "ai_explanation": ai_explanation,
            }
        ),
        200,
    )


@api_bp.route("/customers/<customer_id>/history", methods=["GET"])
def customer_history(customer_id: str) -> tuple[Response, int]:
    """Expose merged local + Nessie transaction baseline history."""
    customer = Customer.query.get(customer_id)
    if not customer:
        return _json_error("customer not found.", 404)

    history, nessie_history_available = _combined_customer_history(customer)
    return (
        jsonify(
            {
                "customer_id": customer.id,
                "nessie_customer_id": customer.nessie_customer_id,
                "total": len(history),
                "nessie_history_available": nessie_history_available,
                "items": history,
            }
        ),
        200,
    )

