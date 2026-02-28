"""Application configuration module for FraudLens."""

import os


class Config:
    """Base configuration for all environments."""

    SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        "sqlite:///fraudlens.db",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    NESSIE_API_KEY = os.getenv("NESSIE_API_KEY", "")
    NESSIE_MOCK_MODE = os.getenv("NESSIE_MOCK_MODE", "true").lower() == "true"
    NESSIE_BASE_URL = os.getenv(
        "NESSIE_BASE_URL",
        "http://api.nessieisreal.com",
    )

    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    GEMINI_BASE_URL = os.getenv(
        "GEMINI_BASE_URL",
        "https://generativelanguage.googleapis.com/v1beta",
    )

    DEFAULT_PAGE_SIZE = int(os.getenv("DEFAULT_PAGE_SIZE", "10"))
    MAX_PAGE_SIZE = int(os.getenv("MAX_PAGE_SIZE", "100"))

