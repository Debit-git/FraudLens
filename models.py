"""Database models for FraudLens."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint

db = SQLAlchemy()


def utc_now() -> datetime:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc)


class Customer(db.Model):
    """Represents an internal customer record linked to Nessie."""

    __tablename__ = "customers"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    nessie_customer_id = db.Column(db.String(64), nullable=True, index=True)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)

    transactions = db.relationship(
        "Transaction",
        back_populates="customer",
        cascade="all, delete-orphan",
        lazy=True,
    )

    def to_dict(self) -> dict:
        """Serialize a customer model instance."""
        return {
            "id": self.id,
            "nessie_customer_id": self.nessie_customer_id,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "created_at": self.created_at.isoformat(),
        }


class Transaction(db.Model):
    """Represents a transaction scored for fraud risk."""

    __tablename__ = "transactions"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    customer_id = db.Column(
        db.String(36),
        db.ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    amount = db.Column(db.Float, nullable=False)
    merchant = db.Column(db.String(255), nullable=False)
    merchant_category = db.Column(db.String(100), nullable=False)
    location = db.Column(db.String(255), nullable=False)
    timestamp = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    fraud_score = db.Column(db.Float, nullable=False, default=0.0)
    risk_level = db.Column(db.String(20), nullable=False, default="LOW")
    risk_factors_json = db.Column(db.Text, nullable=False, default="[]")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)

    customer = db.relationship("Customer", back_populates="transactions", lazy=True)

    @property
    def risk_factors(self) -> list[str]:
        """Return risk factors as a parsed Python list."""
        return json.loads(self.risk_factors_json or "[]")

    @risk_factors.setter
    def risk_factors(self, value: list[str]) -> None:
        """Set risk factors from a Python list."""
        self.risk_factors_json = json.dumps(value or [])

    def to_dict(self) -> dict:
        """Serialize a transaction model instance."""
        return {
            "id": self.id,
            "customer_id": self.customer_id,
            "amount": self.amount,
            "merchant": self.merchant,
            "merchant_category": self.merchant_category,
            "location": self.location,
            "timestamp": self.timestamp.isoformat(),
            "fraud_score": self.fraud_score,
            "risk_level": self.risk_level,
            "risk_factors": self.risk_factors,
            "created_at": self.created_at.isoformat(),
        }


class IdempotencyRecord(db.Model):
    """Stores prior request/response pairs for idempotent POST handling."""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            "method",
            "endpoint",
            name="uq_idempotency_scope",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    idempotency_key = db.Column(db.String(255), nullable=False, index=True)
    method = db.Column(db.String(10), nullable=False)
    endpoint = db.Column(db.String(255), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    response_status = db.Column(db.Integer, nullable=False)
    response_body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)


