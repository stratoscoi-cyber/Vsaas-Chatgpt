"""Flask integration for regions + i18n, shared by every service.

``install_localization`` resolves a per-request language (``?lang=`` or the
``Accept-Language`` header) onto ``g.lang`` and sets the ``Content-Language``
response header. ``register_localization`` mounts discovery endpoints so clients
can enumerate supported languages and African regions and resolve a region from
coordinates.
"""

from __future__ import annotations

from flask import Flask, g, jsonify, request

from . import currency, i18n, regions


def current_lang() -> str:
    return getattr(g, "lang", i18n.DEFAULT_LANGUAGE)


def install_localization(app: Flask, default_language: str = i18n.DEFAULT_LANGUAGE) -> None:
    @app.before_request
    def _resolve_lang():
        g.lang = i18n.resolve_language(
            explicit=request.args.get("lang"),
            accept_language=request.headers.get("Accept-Language"),
            default=default_language,
        )

    @app.after_request
    def _content_language(response):
        if getattr(g, "lang", None):
            response.headers.setdefault("Content-Language", g.lang)
        return response


def register_localization(app: Flask) -> None:
    @app.get("/api/i18n/languages")
    def list_languages():
        return jsonify(languages=i18n.supported_languages(), default=i18n.DEFAULT_LANGUAGE)

    @app.get("/api/i18n/currencies")
    def list_currencies():
        return jsonify(currencies=currency.all_currencies())

    @app.get("/api/i18n/format")
    def format_money():
        try:
            amount = float(request.args["amount"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": {"code": "validation_error", "message": "amount query param required"}}), 422
        code = request.args.get("currency", "USD")
        return jsonify(
            amount=amount, currency=code, locale=current_lang(),
            formatted=currency.format_amount(amount, code, current_lang()),
        )

    @app.get("/api/regions")
    def list_regions():
        return jsonify(
            regions=[r.to_dict() for r in regions.all_regions()],
            default=regions.DEFAULT_REGION.to_dict(),
        )

    @app.get("/api/regions/<code>")
    def get_region(code):
        region = regions.lookup(code)
        if region is None:
            return jsonify({"error": {"code": "not_found", "message": "region not found"}}), 404
        cur = currency.get_currency(region.currency)
        return jsonify(region=region.to_dict(), currency=cur.to_dict())

    @app.get("/api/regions/lookup")
    def lookup_region():
        try:
            lat = float(request.args["lat"])
            lon = float(request.args["lon"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": {"code": "validation_error", "message": "lat and lon query params required"}}), 422
        region = regions.region_for_point(lat, lon)
        return jsonify(
            region=region.to_dict(),
            locale=i18n.resolve_language(region_language=region.primary_language, default=current_lang()),
        )
