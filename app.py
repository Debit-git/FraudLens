"""FraudLens Flask application entrypoint."""

from __future__ import annotations

from flask import Flask, jsonify
from flasgger import Swagger
from dotenv import load_dotenv

# Load environment before importing Config so class attributes read correct values.
load_dotenv(override=True)

from api.routes import api_bp
from api.v1_routes import v1_bp
from config import Config
from models import db
from services.gemini_service import GeminiService
from services.nessie_service import NessieService
from web.routes import web_bp


def create_app() -> Flask:
    """Application factory for FraudLens."""
    app = Flask(__name__)
    app.config.from_object(Config)

    swagger_template = {
        "swagger": "2.0",
        "info": {
            "title": "FraudLens API",
            "version": "1.0.0",
            "description": (
                "Nessie-enriched fraud detection API with resource-oriented fraud checks."
            ),
        },
    }
    swagger_config = {
        "headers": [],
        "specs": [
            {
                "endpoint": "openapi",
                "route": "/openapi.json",
                "rule_filter": lambda rule: True,
                "model_filter": lambda tag: True,
            }
        ],
        "static_url_path": "/flasgger_static",
        "swagger_ui": True,
        "specs_route": "/docs/",
    }
    Swagger(app, template=swagger_template, config=swagger_config)

    db.init_app(app)

    # Service singletons attached through Flask extensions for easy access.
    app.extensions["nessie_service"] = NessieService(
        api_key=app.config["NESSIE_API_KEY"],
        base_url=app.config["NESSIE_BASE_URL"],
        mock_mode=app.config["NESSIE_MOCK_MODE"],
    )
    app.extensions["gemini_service"] = GeminiService(
        api_key=app.config["GEMINI_API_KEY"],
        base_url=app.config["GEMINI_BASE_URL"],
        model=app.config["GEMINI_MODEL"],
    )

    app.register_blueprint(api_bp)
    app.register_blueprint(v1_bp)
    app.register_blueprint(web_bp)

    @app.errorhandler(404)
    def not_found(_err):
        """Handle API and web 404 errors."""
        return jsonify({"error": "resource not found"}), 404

    @app.errorhandler(500)
    def internal_error(_err):
        """Handle unhandled server errors."""
        db.session.rollback()
        return jsonify({"error": "internal server error"}), 500

    with app.app_context():
        db.create_all()

    return app


if __name__ == "__main__":
    application = create_app()
    application.run(debug=True)

