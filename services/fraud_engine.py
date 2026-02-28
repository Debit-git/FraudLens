"""Rule-based fraud scoring engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


MERCHANT_CATEGORIES = {
    "best buy": "electronics",
    "apple": "electronics",
    "walmart": "grocery",
    "target": "grocery",
    "shell": "fuel",
    "chevron": "fuel",
    "uber": "transport",
    "lyft": "transport",
    "amazon": "ecommerce",
    "netflix": "subscription",
}


@dataclass
class FraudAnalysis:
    """Structured fraud analysis output."""

    fraud_score: float
    risk_level: str
    risk_factors: list[str]
    debug_factors: dict[str, float]


def categorize_merchant(merchant: str) -> str:
    """Classify a merchant into a coarse category."""
    merchant_key = (merchant or "").strip().lower()
    for known, category in MERCHANT_CATEGORIES.items():
        if known in merchant_key:
            return category
    return "other"


def _risk_level_for_score(score: float) -> str:
    """Convert normalized score to a level label."""
    if score >= 0.75:
        return "HIGH"
    if score >= 0.4:
        return "MEDIUM"
    return "LOW"


def score_transaction(
    amount: float,
    location: str,
    timestamp: datetime,
    merchant_category: str,
    history: list[dict],
) -> FraudAnalysis:
    """
    Score a transaction using weighted rules.

    Rules:
      - Amount deviation from customer's historical average.
      - Time anomaly for overnight transactions (12am-4am).
      - Location anomaly based on abrupt city change from previous transaction.
      - Merchant category change from historical dominant category.
    """
    amount_weight = 0.4
    time_weight = 0.2
    location_weight = 0.2
    merchant_weight = 0.2

    risk_factors: list[str] = []
    debug_scores: dict[str, float] = {
        "amount_deviation": 0.0,
        "time_anomaly": 0.0,
        "location_anomaly": 0.0,
        "merchant_shift": 0.0,
    }

    # Amount deviation
    if history:
        average_amount = sum(item["amount"] for item in history) / len(history)
        if average_amount > 0:
            ratio = abs(amount - average_amount) / average_amount
            amount_factor = min(1.0, ratio / 2.0)
            debug_scores["amount_deviation"] = amount_factor
            if amount_factor >= 0.5:
                risk_factors.append(
                    f"Amount deviates significantly from average (${average_amount:.2f})."
                )

    # 12am-4am anomaly
    if 0 <= timestamp.hour < 4:
        debug_scores["time_anomaly"] = 1.0
        risk_factors.append("Transaction occurred during high-risk overnight hours.")

    # Location anomaly from previous transaction
    if history:
        previous_location = (history[-1].get("location") or "").strip().lower()
        current_location = (location or "").strip().lower()
        if previous_location and current_location and previous_location != current_location:
            debug_scores["location_anomaly"] = 0.9
            risk_factors.append(
                "Transaction location differs sharply from the previous location."
            )

    # Merchant category shift from dominant category
    if history:
        category_counts: dict[str, int] = {}
        for item in history:
            category = item.get("merchant_category") or "other"
            category_counts[category] = category_counts.get(category, 0) + 1
        dominant_category = max(category_counts, key=category_counts.get)
        if dominant_category != merchant_category:
            debug_scores["merchant_shift"] = 0.7
            risk_factors.append(
                f"Merchant category changed from usual '{dominant_category}' behavior."
            )

    weighted_score = (
        debug_scores["amount_deviation"] * amount_weight
        + debug_scores["time_anomaly"] * time_weight
        + debug_scores["location_anomaly"] * location_weight
        + debug_scores["merchant_shift"] * merchant_weight
    )
    fraud_score = round(max(0.0, min(1.0, weighted_score)), 4)
    risk_level = _risk_level_for_score(fraud_score)

    if not risk_factors:
        risk_factors.append("No significant anomaly detected against customer baseline.")

    return FraudAnalysis(
        fraud_score=fraud_score,
        risk_level=risk_level,
        risk_factors=risk_factors,
        debug_factors=debug_scores,
    )

