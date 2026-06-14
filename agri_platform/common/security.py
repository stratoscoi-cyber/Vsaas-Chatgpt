"""API-key authentication.

When ``auth_enabled`` is set, every request (except exempt paths such as
``/health``) must present a valid key via the ``X-API-Key`` header or an
``Authorization: Bearer <key>`` header. The matched key is stored on ``g`` so
it can be used as the rate-limit bucket.
"""

from __future__ import annotations

from typing import FrozenSet

from flask import Flask, g, jsonify, request


def _extract_key() -> str | None:
    key = request.headers.get("X-API-Key")
    if key:
        return key
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def install_auth(
    app: Flask,
    api_keys: FrozenSet[str],
    enabled: bool,
    exempt_paths: tuple[str, ...] = ("/health",),
) -> None:
    if not enabled:
        return
    if not api_keys:
        app.logger.warning("AUTH_ENABLED is true but no API_KEYS configured; all requests will be rejected")

    @app.before_request
    def _authenticate():
        if request.path in exempt_paths or request.method == "OPTIONS":
            return None
        key = _extract_key()
        if not key or key not in api_keys:
            return (
                jsonify({"error": {"code": "unauthorized", "message": "valid API key required"}}),
                401,
            )
        g.api_key = key
        return None
