"""WSGI entrypoint for gunicorn: ``agri_platform.traas.wsgi:app``."""

from .app import create_app

app = create_app()
