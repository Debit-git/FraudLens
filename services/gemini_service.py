"""Google Gemini API integration for narrative fraud explanations."""

from __future__ import annotations

import json

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
            "fraudulent. Be concise and professional.\n\n"
            f"Data:\n{json.dumps(payload, indent=2)}"
        )

    def generate_explanation(self, payload: dict) -> str:
        """Generate natural-language fraud explanation via Gemini."""
        self._ensure_configured()
        prompt = self.build_prompt(payload)
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
                "maxOutputTokens": 180,
            },
        }
        try:
            response = requests.post(url, json=body, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise GeminiServiceError(f"Gemini request failed: {exc}") from exc

        try:
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise GeminiServiceError("Unexpected Gemini response structure.") from exc

