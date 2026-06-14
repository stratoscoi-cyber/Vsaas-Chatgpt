"""Consistent JSON error handling for the Flask services.

All client/server failures are funnelled into a single envelope::

    {"error": {"code": "not_found", "message": "...", "details": {...}}}

so API consumers can rely on one shape regardless of which endpoint failed.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException


class ApiError(Exception):
    """A controlled error that maps to an HTTP response."""

    def __init__(
        self,
        message: str,
        status_code: int = 400,
        code: str = "bad_request",
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.details = details or {}

    def to_payload(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            body["error"]["details"] = self.details
        return body


# --- request parsing / validation helpers -------------------------------------

def get_json() -> Dict[str, Any]:
    """Return the JSON body or raise a 400 ``ApiError`` (never ``None``)."""
    data = request.get_json(silent=True)
    if data is None:
        # Tolerate an empty body for endpoints with all-optional fields.
        if not request.data:
            return {}
        raise ApiError("request body must be valid JSON", code="invalid_json")
    if not isinstance(data, dict):
        raise ApiError("request body must be a JSON object", code="invalid_json")
    return data


def require(data: Dict[str, Any], *fields: str) -> None:
    """Raise a 422 listing any required fields that are missing/empty."""
    missing = [f for f in fields if data.get(f) in (None, "", [], {})]
    if missing:
        raise ApiError(
            "missing required field(s)",
            status_code=422,
            code="validation_error",
            details={"missing": missing},
        )


def as_float(data: Dict[str, Any], field: str) -> float:
    try:
        return float(data[field])
    except (KeyError, TypeError, ValueError):
        raise ApiError(
            f"field '{field}' must be a number",
            status_code=422,
            code="validation_error",
            details={"field": field},
        )


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ApiError)
    def _handle_api_error(exc: ApiError):
        return jsonify(exc.to_payload()), exc.status_code

    @app.errorhandler(HTTPException)
    def _handle_http(exc: HTTPException):
        code = exc.name.lower().replace(" ", "_")
        return jsonify({"error": {"code": code, "message": exc.description}}), exc.code

    @app.errorhandler(Exception)
    def _handle_unexpected(exc: Exception):  # noqa: ANN001
        app.logger.exception("unhandled error: %s", exc)
        return (
            jsonify({"error": {"code": "internal_error", "message": "internal server error"}}),
            500,
        )
