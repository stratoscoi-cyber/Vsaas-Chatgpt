"""Notification engine for WFAAS alerts.

Alerts are dispatched to farm owners according to their stored
:class:`~agri_platform.wfaas.models.AlertPreference`. The transport is
pluggable: a real SMTP sender for production and a console/recording sender for
development and tests.
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.text import MIMEText
from typing import List, Optional, Protocol

from .models import AlertPreference, Farm, WeatherAlert

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    def send(self, to: str, subject: str, body: str) -> bool: ...


class ConsoleNotifier:
    """Logs notifications and records them (handy for tests/dev)."""

    def __init__(self) -> None:
        self.sent: List[dict] = []

    def send(self, to: str, subject: str, body: str) -> bool:
        self.sent.append({"to": to, "subject": subject, "body": body})
        logger.info("notification (console) to=%s subject=%s", to, subject)
        return True


class SMTPNotifier:
    """Sends email via SMTP."""

    def __init__(self, host, port, sender, user=None, password=None, use_tls=False, timeout=15):
        self.host = host
        self.port = port
        self.sender = sender
        self.user = user
        self.password = password
        self.use_tls = use_tls
        self.timeout = timeout

    def send(self, to: str, subject: str, body: str) -> bool:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = to
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as server:
                if self.use_tls:
                    server.starttls()
                if self.user:
                    server.login(self.user, self.password or "")
                server.sendmail(self.sender, [to], msg.as_string())
            return True
        except (smtplib.SMTPException, OSError) as exc:
            logger.error("failed to send email to %s: %s", to, exc)
            return False


def make_notifier(settings) -> Notifier:
    if settings.notifications_enabled and settings.smtp_host:
        return SMTPNotifier(
            host=settings.smtp_host,
            port=settings.smtp_port,
            sender=settings.smtp_from,
            user=settings.smtp_user,
            password=settings.smtp_password,
            use_tls=settings.smtp_use_tls,
        )
    return ConsoleNotifier()


def _format_alert(alert: WeatherAlert, farm: Optional[Farm]) -> str:
    lines = [
        f"Alert type : {alert.alert_type}",
        f"Severity   : {alert.severity}",
        f"Message    : {alert.message}",
        f"Farm       : {alert.farm_id}" + (f" ({farm.name})" if farm and farm.name else ""),
        f"Location   : {alert.coordinates}",
        f"Confidence : {alert.confidence:.2f}" if alert.confidence is not None else "Confidence : n/a",
        f"Time (UTC) : {alert.timestamp}",
    ]
    return "\n".join(lines)


def dispatch_alert(session, notifier: Notifier, alert: WeatherAlert) -> bool:
    """Notify a farm owner about an alert, honouring their preferences.

    Returns ``True`` if a notification was sent, ``False`` if suppressed (no
    recipient, channel disabled, or alert type opted out).
    """
    farm = session.query(Farm).filter_by(farm_id=alert.farm_id).first()
    if farm is None or not farm.owner_email:
        return False
    pref = session.query(AlertPreference).filter_by(farm_id=alert.farm_id).first()
    if pref is not None:
        if not pref.email_enabled:
            return False
        if pref.alert_types and alert.alert_type not in pref.alert_types:
            return False
    subject = f"[AgriTech] {alert.severity} {alert.alert_type.replace('_', ' ')}"
    return notifier.send(farm.owner_email, subject, _format_alert(alert, farm))
