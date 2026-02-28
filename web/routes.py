"""Web dashboard routes for demo purposes."""

from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, flash, redirect, render_template, request, url_for

from models import Customer, Transaction, db
from services.fraud_engine import categorize_merchant, score_transaction

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


@web_bp.route("/", methods=["GET"])
def home():
    """List customers with quick stats."""
    customers = Customer.query.order_by(Customer.created_at.desc()).all()
    customer_rows = []
    for customer in customers:
        tx_count = Transaction.query.filter_by(customer_id=customer.id).count()
        customer_rows.append({"customer": customer, "tx_count": tx_count})
    return render_template("index.html", customer_rows=customer_rows)


@web_bp.route("/simulate", methods=["GET", "POST"])
def simulate():
    """Render and process a simulation form."""
    customers = Customer.query.order_by(Customer.created_at.desc()).all()
    if request.method == "GET":
        return render_template("simulate.html", customers=customers)

    customer_id = (request.form.get("customer_id") or "").strip()
    merchant = (request.form.get("merchant") or "").strip()
    location = (request.form.get("location") or "").strip()
    amount_raw = request.form.get("amount")
    timestamp_raw = request.form.get("timestamp")

    if not customer_id or not merchant or not location:
        flash("Customer, merchant, and location are required.", "error")
        return render_template("simulate.html", customers=customers), 400

    try:
        amount = float(amount_raw)
        if amount <= 0:
            raise ValueError("amount invalid")
    except (TypeError, ValueError):
        flash("Amount must be a positive number.", "error")
        return render_template("simulate.html", customers=customers), 400

    try:
        timestamp = _parse_timestamp(timestamp_raw)
    except (TypeError, ValueError):
        flash("Timestamp must be a valid ISO date/time.", "error")
        return render_template("simulate.html", customers=customers), 400

    customer = Customer.query.get(customer_id)
    if not customer:
        flash("Customer not found.", "error")
        return render_template("simulate.html", customers=customers), 404

    history = (
        Transaction.query.filter_by(customer_id=customer_id)
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

    return redirect(url_for("web.result", transaction_id=transaction.id))


@web_bp.route("/result/<transaction_id>", methods=["GET"])
def result(transaction_id: str):
    """Display simulation output in dashboard format."""
    transaction = Transaction.query.get_or_404(transaction_id)
    customer = Customer.query.get(transaction.customer_id)
    return render_template(
        "result.html",
        transaction=transaction,
        customer=customer,
        risk_factors=transaction.risk_factors,
    )

