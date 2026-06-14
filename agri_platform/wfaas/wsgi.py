"""WSGI entrypoint for gunicorn: ``agri_platform.wfaas.wsgi:app``."""

from .app import create_app

app = create_app()
