"""Web dashboard routes for demo purposes."""

from __future__ import annotations

from datetime import datetime, timezone

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from models import Customer, FraudCheck, Transaction, db
from services.fraud_engine import categorize_merchant, score_transaction
from services.gemini_service import GeminiServiceError
from services.nessie_service import NessieServiceError

web_bp = Blueprint("web", __name__)


def _decision_for_risk_level(risk_level: str | None) -> str:
    """Map risk level to an action recommendation for operators."""
    level = (risk_level or "").upper()
    if level == "HIGH":
        return "DECLINE"
    if level == "MEDIUM":
        return "REVIEW"
    return "ALLOW"


def _parse_timestamp(value: str) -> datetime:
    """Parse timestamp from form data."""
    if not value:
        raise ValueError("timestamp is required")
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _simulate_customer_options() -> tuple[list[dict], str | None]:
    """Return customer options for simulate form (Nessie-first with local fallback)."""
    nessie = current_app.extensions["nessie_service"]
    try:
        remote_customers = nessie.list_customers(limit=200)
        options = []
        for remote in remote_customers:
            first = remote.get("first_name") or ""
            last = remote.get("last_name") or ""
            options.append(
                {
                    "id": remote["nessie_customer_id"],
                    "display": f"{first} {last}".strip() or remote["nessie_customer_id"],
                    "source": "nessie",
                }
            )
        return options, None
    except NessieServiceError as exc:
        customers = Customer.query.order_by(Customer.created_at.desc()).all()
        options = [
            {
                "id": customer.id,
                "display": f"{customer.first_name} {customer.last_name}".strip(),
                "source": "local",
            }
            for customer in customers
        ]
        return options, str(exc)


def _resolve_or_create_local_customer(customer_ref: str) -> Customer | None:
    """
    Resolve a customer reference to a local row.

    `customer_ref` can be either:
      - local customer UUID
      - Nessie customer id
    """
    customer = Customer.query.get(customer_ref)
    if customer:
        return customer

    customer = Customer.query.filter_by(nessie_customer_id=customer_ref).first()
    if customer:
        return customer

    nessie = current_app.extensions["nessie_service"]
    try:
        remote_customer = nessie.get_customer(customer_ref)
    except NessieServiceError:
        return None
    if not remote_customer:
        return None

    customer = Customer(
        first_name=(remote_customer.get("first_name") or "Nessie").strip() or "Nessie",
        last_name=(remote_customer.get("last_name") or "Customer").strip() or "Customer",
        nessie_customer_id=remote_customer["nessie_customer_id"],
    )
    db.session.add(customer)
    db.session.commit()
    return customer


def _merged_history_for_customer(customer: Customer) -> tuple[list[dict], bool]:
    """Build merged local + Nessie transaction history for simulation scoring."""
    local_history = (
        Transaction.query.filter_by(customer_id=customer.id).order_by(Transaction.timestamp).all()
    )
    local_items = [
        {
            "id": tx.id,
            "amount": tx.amount,
            "location": tx.location,
            "merchant_category": tx.merchant_category,
            "timestamp": tx.timestamp.isoformat(),
            "source": "local",
            "merchant": tx.merchant,
        }
        for tx in local_history
    ]

    nessie_items: list[dict] = []
    nessie_ok = False
    if customer.nessie_customer_id:
        nessie = current_app.extensions["nessie_service"]
        try:
            nessie_items = nessie.get_customer_history(customer.nessie_customer_id)
            for item in nessie_items:
                if item.get("merchant"):
                    item["merchant_category"] = categorize_merchant(item["merchant"])
            nessie_ok = True
        except NessieServiceError:
            nessie_ok = False

    merged = local_items + nessie_items
    merged.sort(key=lambda item: item.get("timestamp", ""))
    return merged, nessie_ok


@web_bp.route("/", methods=["GET"])
def home():
    """List dashboard stats and customers (Nessie-first)."""
    customers = Customer.query.order_by(Customer.created_at.desc()).all()
    local_rows = []
    local_by_nessie_id: dict[str, dict] = {}
    for customer in customers:
        tx_count = Transaction.query.filter_by(customer_id=customer.id).count()
        row = {"customer": customer, "tx_count": tx_count}
        local_rows.append(row)
        if customer.nessie_customer_id:
            local_by_nessie_id[customer.nessie_customer_id] = row

    customer_rows = local_rows
    customer_source = "local"
    nessie_error = None
    nessie = current_app.extensions["nessie_service"]
    try:
        remote_customers = nessie.list_customers(limit=200)
        remote_rows = []
        for remote in remote_customers:
            linked_local = local_by_nessie_id.get(remote["nessie_customer_id"])
            try:
                remote_history = nessie.get_customer_history(remote["nessie_customer_id"])
                remote_tx_count = len(remote_history)
            except NessieServiceError:
                remote_tx_count = linked_local["tx_count"] if linked_local else 0
            remote_rows.append(
                {
                    "first_name": remote.get("first_name") or "",
                    "last_name": remote.get("last_name") or "",
                    "local_id": linked_local["customer"].id if linked_local else None,
                    "nessie_customer_id": remote["nessie_customer_id"],
                    "tx_count": remote_tx_count,
                }
            )
        customer_rows = remote_rows
        customer_source = "nessie"
    except NessieServiceError as exc:
        nessie_error = str(exc)

    recent_checks = FraudCheck.query.order_by(FraudCheck.created_at.desc()).limit(12).all()
    total_fraud_checks = FraudCheck.query.count()
    high_risk_checks = FraudCheck.query.filter_by(risk_level="HIGH").count()

    return render_template(
        "index.html",
        customer_rows=customer_rows,
        customer_source=customer_source,
        nessie_error=nessie_error,
        recent_checks=recent_checks,
        total_fraud_checks=total_fraud_checks,
        high_risk_checks=high_risk_checks,
    )


@web_bp.route("/simulate", methods=["GET", "POST"])
def simulate():
    """Render and process a simulation form."""
    customer_options, nessie_error = _simulate_customer_options()
    if request.method == "GET":
        return render_template(
            "simulate.html",
            customer_options=customer_options,
            nessie_error=nessie_error,
        )

    customer_id = (request.form.get("customer_id") or "").strip()
    merchant = (request.form.get("merchant") or "").strip()
    location = (request.form.get("location") or "").strip()
    amount_raw = request.form.get("amount")
    timestamp_raw = request.form.get("timestamp")

    if not customer_id or not merchant or not location:
        flash("Customer, merchant, and location are required.", "error")
        return (
            render_template(
                "simulate.html",
                customer_options=customer_options,
                nessie_error=nessie_error,
            ),
            400,
        )

    try:
        amount = float(amount_raw)
        if amount <= 0:
            raise ValueError("amount invalid")
    except (TypeError, ValueError):
        flash("Amount must be a positive number.", "error")
        return (
            render_template(
                "simulate.html",
                customer_options=customer_options,
                nessie_error=nessie_error,
            ),
            400,
        )

    try:
        timestamp = _parse_timestamp(timestamp_raw)
    except (TypeError, ValueError):
        flash("Timestamp must be a valid ISO date/time.", "error")
        return (
            render_template(
                "simulate.html",
                customer_options=customer_options,
                nessie_error=nessie_error,
            ),
            400,
        )

    customer = _resolve_or_create_local_customer(customer_id)
    if not customer:
        flash("Customer not found.", "error")
        return (
            render_template(
                "simulate.html",
                customer_options=customer_options,
                nessie_error=nessie_error,
            ),
            404,
        )

    history_dicts, _ = _merged_history_for_customer(customer)

    merchant_category = categorize_merchant(merchant)
    analysis = score_transaction(
        amount=amount,
        location=location,
        timestamp=timestamp,
        merchant_category=merchant_category,
        history=history_dicts,
    )

    transaction = Transaction(
        customer_id=customer.id,
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

    return redirect(url_for("web.result", transaction_id=transaction.id))


@web_bp.route("/simulate/customer-profile", methods=["GET"])
def simulate_customer_profile():
    """Return customer behavior profile and quick-fill simulation presets."""
    customer_ref = (request.args.get("customer_id") or "").strip()
    if not customer_ref:
        return jsonify({"error": "customer_id is required"}), 400

    customer = _resolve_or_create_local_customer(customer_ref)
    if not customer:
        return jsonify({"error": "customer not found"}), 404

    history, nessie_history_available = _merged_history_for_customer(customer)
    if not history:
        return jsonify(
            {
                "customer_id": customer.id,
                "nessie_customer_id": customer.nessie_customer_id,
                "history_count": 0,
                "nessie_history_available": nessie_history_available,
            }
        ), 200

    amounts = [float(item.get("amount") or 0) for item in history]
    avg_amount = round(sum(amounts) / len(amounts), 2) if amounts else 0.0
    max_amount = round(max(amounts), 2) if amounts else 0.0

    category_counts: dict[str, int] = {}
    location_counts: dict[str, int] = {}
    merchant_counts: dict[str, int] = {}
    for item in history:
        category = (item.get("merchant_category") or "other").strip().lower()
        category_counts[category] = category_counts.get(category, 0) + 1
        location = (item.get("location") or "nessie-unknown").strip()
        location_counts[location] = location_counts.get(location, 0) + 1
        merchant = (item.get("merchant") or "").strip()
        if merchant:
            merchant_counts[merchant] = merchant_counts.get(merchant, 0) + 1

    top_category = max(category_counts, key=category_counts.get) if category_counts else "other"
    top_location = max(location_counts, key=location_counts.get) if location_counts else "nessie-unknown"
    top_merchant = max(merchant_counts, key=merchant_counts.get) if merchant_counts else "Amazon"

    normal_amount = max(1.0, round(avg_amount if avg_amount > 0 else 35.0, 2))
    suspicious_amount = max(10.0, round(max_amount * 1.8 if max_amount > 0 else 900.0, 2))
    suspicious_merchant = "Best Buy" if top_category != "electronics" else "Crypto Exchange"
    suspicious_location = "International-Unknown" if top_location != "International-Unknown" else "Far-Away City"

    return jsonify(
        {
            "customer_id": customer.id,
            "nessie_customer_id": customer.nessie_customer_id,
            "history_count": len(history),
            "nessie_history_available": nessie_history_available,
            "avg_amount": avg_amount,
            "max_amount": max_amount,
            "top_category": top_category,
            "top_location": top_location,
            "top_merchant": top_merchant,
            "scenario_presets": {
                "normal": {
                    "amount": normal_amount,
                    "merchant": top_merchant,
                    "location": top_location,
                    "timestamp": "2026-06-15T14:30:00Z",
                },
                "suspicious": {
                    "amount": suspicious_amount,
                    "merchant": suspicious_merchant,
                    "location": suspicious_location,
                    "timestamp": "2026-06-15T02:30:00Z",
                },
            },
        }
    ), 200


@web_bp.route("/result/<transaction_id>", methods=["GET"])
def result(transaction_id: str):
    """Display simulation output in dashboard format."""
    transaction = Transaction.query.get_or_404(transaction_id)
    customer = Customer.query.get(transaction.customer_id)
    gemini = current_app.extensions["gemini_service"]
    explanation_payload = {
        "transaction": transaction.to_dict(),
        "customer": customer.to_dict() if customer else {"id": transaction.customer_id},
        "risk_factors": transaction.risk_factors,
    }
    try:
        ai_explanation = gemini.generate_explanation(explanation_payload)
    except GeminiServiceError:
        ai_explanation = (
            "AI explanation is temporarily unavailable. "
            "Risk factors shown above are based on rule analysis."
        )

    return render_template(
        "result.html",
        transaction=transaction,
        customer=customer,
        risk_factors=transaction.risk_factors,
        ai_explanation=ai_explanation,
        decision=_decision_for_risk_level(transaction.risk_level),
    )


@web_bp.route("/fraud-check/<fraud_check_id>", methods=["GET"])
def fraud_check_result(fraud_check_id: str):
    """Display a stored v1 fraud-check including Gemini explanation."""
    fraud_check = FraudCheck.query.get_or_404(fraud_check_id)
    customer = Customer.query.get(fraud_check.customer_id)
    if not customer:
        customer = Customer.query.filter_by(nessie_customer_id=fraud_check.customer_id).first()

    return render_template(
        "fraud_check_result.html",
        fraud_check=fraud_check,
        customer=customer,
        risk_factors=fraud_check.risk_factors,
        ai_explanation=fraud_check.ai_explanation
        or "AI explanation is unavailable for this fraud check.",
        decision=_decision_for_risk_level(fraud_check.risk_level),
    )



