"""Web dashboard routes for demo purposes."""

from __future__ import annotations

from datetime import datetime, timezone

from flask import (
    Blueprint,
    current_app,
    flash,
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
            remote_rows.append(
                {
                    "first_name": remote.get("first_name") or "",
                    "last_name": remote.get("last_name") or "",
                    "local_id": linked_local["customer"].id if linked_local else None,
                    "nessie_customer_id": remote["nessie_customer_id"],
                    "tx_count": linked_local["tx_count"] if linked_local else 0,
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

    history = (
        Transaction.query.filter_by(customer_id=customer.id)
        .order_by(Transaction.timestamp)
        .all()
    )
    history_dicts = [
        {
            "amount": tx.amount,
            "location": tx.location,
            "merchant_category": tx.merchant_category,
            "timestamp": tx.timestamp.isoformat(),
        }
        for tx in history
    ]

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
    )



