"""Client wrapper for Capital One Nessie API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

import requests


class NessieServiceError(Exception):
    """Raised when Nessie API interaction fails."""


@dataclass
class NessieCustomerResponse:
    """Represents the relevant output of a Nessie customer creation call."""

    customer_id: str
    raw: dict


class NessieService:
    """Small service class to encapsulate Nessie HTTP requests."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout: int = 10,
        mock_mode: bool = False,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.mock_mode = mock_mode

    def _ensure_configured(self) -> None:
        if not self.api_key:
            raise NessieServiceError("Nessie API key is missing.")

    def _parse_nessie_date(self, value: str | None) -> str:
        """Normalize Nessie date strings into ISO-8601 UTC."""
        if not value:
            return datetime.now(timezone.utc).isoformat()
        try:
            # Nessie commonly returns YYYY-MM-DD for purchase_date.
            if len(value) == 10:
                parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                return parsed.isoformat()
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
                timezone.utc
            ).isoformat()
        except ValueError:
            return datetime.now(timezone.utc).isoformat()

    def _get(self, path: str) -> list | dict:
        """Execute a GET call against Nessie and return JSON payload."""
        self._ensure_configured()
        url = f"{self.base_url}{path}"
        separator = "&" if "?" in path else "?"
        url = f"{url}{separator}key={self.api_key}"
        try:
            response = requests.get(url, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise NessieServiceError(f"Failed Nessie request {path}: {exc}") from exc

    def create_customer(self, first_name: str, last_name: str) -> NessieCustomerResponse:
        """Create a customer in Nessie and return the created remote identifier."""
        if self.mock_mode:
            fake_id = f"mock-{uuid.uuid4().hex[:16]}"
            return NessieCustomerResponse(
                customer_id=fake_id,
                raw={
                    "mocked": True,
                    "objectCreated": {
                        "_id": fake_id,
                        "first_name": first_name,
                        "last_name": last_name,
                    },
                },
            )

        self._ensure_configured()
        url = f"{self.base_url}/customers?key={self.api_key}"
        payload = {
            "first_name": first_name,
            "last_name": last_name,
            # Nessie customer creation requires an address object.
            "address": {
                "street_number": "1",
                "street_name": "Main St",
                "city": "Chicago",
                "state": "IL",
                "zip": "60601",
            },
        }
        try:
            response = requests.post(url, json=payload, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise NessieServiceError(f"Failed to create customer in Nessie: {exc}") from exc

        created = data.get("objectCreated") or data
        customer_id = created.get("_id") or created.get("id")
        if not customer_id:
            raise NessieServiceError("Nessie response did not contain a customer id.")

        return NessieCustomerResponse(customer_id=customer_id, raw=data)

    def list_customers(self, limit: int = 100) -> list[dict]:
        """Fetch customers from Nessie and normalize shape."""
        if self.mock_mode:
            return []

        payload = self._get("/customers")
        customers = payload if isinstance(payload, list) else []
        normalized: list[dict] = []
        for customer in customers[: max(1, limit)]:
            address = customer.get("address") or {}
            normalized.append(
                {
                    "nessie_customer_id": customer.get("_id") or customer.get("id"),
                    "first_name": customer.get("first_name", "").strip(),
                    "last_name": customer.get("last_name", "").strip(),
                    "address": {
                        "street_number": address.get("street_number"),
                        "street_name": address.get("street_name"),
                        "city": address.get("city"),
                        "state": address.get("state"),
                        "zip": address.get("zip"),
                    },
                }
            )
        return [item for item in normalized if item["nessie_customer_id"]]

    def get_customer_history(self, nessie_customer_id: str) -> list[dict]:
        """
        Fetch and normalize purchase history from Nessie for a customer.

        Nessie model:
          customer -> accounts -> purchases
        """
        if self.mock_mode or not nessie_customer_id:
            return []

        accounts_payload = self._get(f"/customers/{nessie_customer_id}/accounts")
        accounts = accounts_payload if isinstance(accounts_payload, list) else []

        history: list[dict] = []
        for account in accounts:
            account_id = account.get("_id")
            if not account_id:
                continue
            purchases_payload = self._get(f"/accounts/{account_id}/purchases")
            purchases = purchases_payload if isinstance(purchases_payload, list) else []
            for purchase in purchases:
                merchant_name = (
                    purchase.get("description")
                    or purchase.get("merchant")
                    or purchase.get("merchant_id")
                    or "Nessie Merchant"
                )
                history.append(
                    {
                        "id": purchase.get("_id") or purchase.get("id") or uuid.uuid4().hex,
                        "amount": float(purchase.get("amount", 0) or 0),
                        "location": "nessie-unknown",
                        "merchant_category": "other",
                        "timestamp": self._parse_nessie_date(purchase.get("purchase_date")),
                        "source": "nessie",
                        "merchant": merchant_name,
                    }
                )

        history.sort(key=lambda item: item["timestamp"])
        return history

