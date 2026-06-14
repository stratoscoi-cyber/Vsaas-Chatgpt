"""Structured logging and per-request correlation IDs."""

from __future__ import annotations

import json
import logging
import time
import uuid

from flask import Flask, g, request


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON for log aggregation."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "method", "path", "status", "duration_ms", "service"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: str = "INFO", json_format: bool = True) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if json_format else logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    ))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def install_request_logging(app: Flask, service: str) -> None:
    """Attach a request id to every request and emit an access log line."""

    @app.before_request
    def _start():
        g.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        g.start_time = time.perf_counter()

    @app.after_request
    def _finish(response):
        duration_ms = round((time.perf_counter() - getattr(g, "start_time", time.perf_counter())) * 1000, 2)
        response.headers["X-Request-ID"] = getattr(g, "request_id", "")
        app.logger.info(
            "request",
            extra={
                "request_id": getattr(g, "request_id", None),
                "method": request.method,
                "path": request.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
                "service": service,
            },
        )
        return response
