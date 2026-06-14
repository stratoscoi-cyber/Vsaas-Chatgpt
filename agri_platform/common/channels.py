"""Outbound notification channels (SMS / WhatsApp / push) as pluggable adapters.

The default channel logs (and records) messages; real delivery is via an HTTP
provider webhook configured per channel. No messages are sent to real recipients
unless a provider URL is configured — nothing is faked.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Protocol

import requests

logger = logging.getLogger(__name__)


class Channel(Protocol):
    kind: str
    def send(self, to: str, message: str, meta: Optional[Dict] = None) -> bool: ...


class ConsoleChannel:
    def __init__(self, kind: str = "console"):
        self.kind = kind
        self.sent: List[Dict] = []

    def send(self, to: str, message: str, meta: Optional[Dict] = None) -> bool:
        self.sent.append({"kind": self.kind, "to": to, "message": message, "meta": meta or {}})
        logger.info("notify[%s] to=%s: %s", self.kind, to, message)
        return True


class HttpChannel:
    """Generic provider channel (e.g. SMS gateway or WhatsApp Business API)."""

    def __init__(self, kind: str, url: str, timeout: float = 10.0):
        self.kind = kind
        self.url = url
        self.timeout = timeout
        self._fallback = ConsoleChannel(kind)

    def send(self, to: str, message: str, meta: Optional[Dict] = None) -> bool:
        try:
            requests.post(self.url, json={"channel": self.kind, "to": to, "message": message,
                                          "meta": meta or {}}, timeout=self.timeout)
            return True
        except requests.RequestException as exc:
            logger.warning("channel %s delivery failed: %s", self.kind, exc)
            return self._fallback.send(to, message, meta)


def build_channels(config: Dict[str, Optional[str]]) -> Dict[str, Channel]:
    """``{"sms": url|None, "whatsapp": url|None, "push": url|None}`` -> channels."""
    channels: Dict[str, Channel] = {}
    for kind, url in config.items():
        channels[kind] = HttpChannel(kind, url) if url else ConsoleChannel(kind)
    return channels
