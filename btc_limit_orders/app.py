"""Flask routes for the Bitcoin Live Outlook dashboard."""

import math
import time
from datetime import datetime

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import BadRequest, UnsupportedMediaType

from dashboard import DashboardService, EASTERN_TIME, OUTCOME_WINDOWS


def create_app(service: DashboardService) -> Flask:
    """Create the web application around an already-configured runtime service."""
    app = Flask(__name__)
    app.extensions["dashboard_service"] = service

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/state")
    def state():
        raw_window = request.args.get("window", "86400")
        if raw_window == "all":
            window_seconds = None
        else:
            try:
                window_seconds = int(raw_window)
            except ValueError:
                return jsonify(
                    error="window must be a supported number of seconds or 'all'"
                ), 400
            if window_seconds not in OUTCOME_WINDOWS:
                return jsonify(error="unsupported outcome time window"), 400
        return jsonify(service.snapshot(window_seconds))

    @app.post("/api/settings")
    def settings():
        try:
            payload = request.get_json()
        except (BadRequest, UnsupportedMediaType):
            return jsonify(error="request body must be valid JSON"), 400
        if not isinstance(payload, dict):
            return jsonify(error="request body must be a JSON object"), 400
        try:
            return jsonify(service.set_settings(payload))
        except ValueError as error:
            return jsonify(error=str(error)), 400

    @app.post("/api/scenario-likelihood")
    def scenario_likelihood():
        try:
            payload = request.get_json()
        except (BadRequest, UnsupportedMediaType):
            return jsonify(error="request body must be valid JSON"), 400
        if not isinstance(payload, dict):
            return jsonify(error="request body must be a JSON object"), 400

        raw_target_time = payload.get("target_datetime")
        if not isinstance(raw_target_time, str):
            return jsonify(error="target_datetime is required"), 400
        try:
            target_time = datetime.fromisoformat(raw_target_time)
        except ValueError:
            return jsonify(error="target_datetime must be a valid ISO date and time"), 400
        if target_time.tzinfo is None:
            target_time = target_time.replace(tzinfo=EASTERN_TIME)
        if target_time.timestamp() <= time.time():
            return jsonify(error="target date and time must be in the future"), 400

        direction = payload.get("direction")
        if direction not in ("above", "below"):
            return jsonify(error="direction must be 'above' or 'below'"), 400
        try:
            target_price = float(payload.get("target_price"))
        except (TypeError, ValueError):
            return jsonify(error="target_price must be a positive number"), 400
        if not math.isfinite(target_price) or target_price <= 0:
            return jsonify(error="target_price must be a positive number"), 400
        try:
            return jsonify(
                service.scenario_likelihood(target_time, target_price, direction)
            )
        except RuntimeError as error:
            return jsonify(error=str(error)), 503
        except ValueError as error:
            return jsonify(error=str(error)), 400

    @app.get("/health")
    def health():
        started, error = service.health_status()
        if error:
            return jsonify(status="error", detail=error), 503
        return jsonify(status="ok", started=started)

    return app
