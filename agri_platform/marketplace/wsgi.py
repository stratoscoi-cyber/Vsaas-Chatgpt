"""WSGI entrypoint for gunicorn: ``agri_platform.marketplace.wsgi:app``."""

from .app import create_app

app = create_app()
