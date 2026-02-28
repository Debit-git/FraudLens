"""Google Gemini API integration for narrative fraud explanations."""

from __future__ import annotations

import json
import re
import time

import requests


class GeminiServiceError(Exception):
    """Raised when Gemini explanation generation fails."""


class GeminiService:
    """Encapsulates calls to Gemini text generation API."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: int = 12,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def _ensure_configured(self) -> None:
        if not self.api_key:
            raise GeminiServiceError("Gemini API key is missing.")

    def build_prompt(self, payload: dict) -> str:
        """Build a concise, structured fraud analysis prompt."""
        return (
            "You are a financial fraud analyst. Given this transaction data and "
            "baseline behavior, explain why this transaction may or may not be "
            "fraudulent. Be concise and professional. "
            "Return plain text only, 3-5 complete sentences, no markdown.\n\n"
            f"Data:\n{json.dumps(payload, indent=2)}"
        )

    def _call_generate(self, prompt: str, max_tokens: int) -> str:
        """Call Gemini once and return the extracted text."""
        url = (
            f"{self.base_url}/models/{self.model}:generateContent"
            f"?key={self.api_key}"
        )
        body = {
            "contents": [
                {
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": max_tokens,
            },
        }
        try:
            response = requests.post(url, json=body, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise GeminiServiceError(f"Gemini request failed: {exc}") from exc

        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise GeminiServiceError("Unexpected Gemini response structure.") from exc
        return (text or "").strip()

    def _finalize_explanation(self, text: str, payload: dict) -> str:
        """Normalize and guarantee a complete explanation sentence block."""
        cleaned = (text or "").strip().replace("\n", " ")
        cleaned = re.sub(r"\s+", " ", cleaned)
        if not cleaned:
            cleaned = ""

        # Keep only complete sentences when possible.
        last_stop = max(cleaned.rfind("."), cleaned.rfind("!"), cleaned.rfind("?"))
        if last_stop >= 40:
            cleaned = cleaned[: last_stop + 1].strip()

        if len(cleaned) >= 80 and cleaned.endswith((".", "!", "?")):
            return cleaned

        transaction = payload.get("transaction") or {}
        amount = transaction.get("amount", "N/A")
        merchant = transaction.get("merchant", "unknown merchant")
        location = transaction.get("location", "unknown location")
        return (
            f"This fraud check was completed for a ${amount} transaction at {merchant} "
            f"in {location}. Risk is assessed using amount deviation, transaction timing, "
            "location change, and merchant-category shift against baseline behavior. "
            "Review the risk score and factors to determine whether manual investigation is needed."
        )

    def generate_explanation(self, payload: dict) -> str:
        """Generate natural-language fraud explanation via Gemini."""
        self._ensure_configured()
        prompt = self.build_prompt(payload)
        start = time.monotonic()

        primary = self._call_generate(prompt=prompt, max_tokens=320)
        if len(primary) >= 80 and primary.endswith((".", "!", "?")):
            return self._finalize_explanation(primary, payload)

        # Keep endpoint responsive in interactive tools (Swagger/Postman).
        if (time.monotonic() - start) > 4.0:
            return self._finalize_explanation(primary, payload)

        # Retry once with a stricter prompt if first output is clipped/too short.
        retry_prompt = (
            prompt
            + "\n\nRewrite the explanation as complete plain-text sentences and "
            "ensure the final sentence ends with a period."
        )
        secondary = self._call_generate(prompt=retry_prompt, max_tokens=420)
        candidate = secondary or primary
        return self._finalize_explanation(candidate, payload)

