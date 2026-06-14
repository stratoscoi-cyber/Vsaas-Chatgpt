"""KYC / document-verification & sanctions-screening integration.

Turning a submitted document's ``verified`` flag from an operator assertion into a
genuine verification requires an external provider (OCR + issuer/registry checks,
sanctions/PEP screening). This module is the **integration boundary**: the default
provider verifies nothing automatically (it returns ``manual_review_required`` so a
human still decides), and an HTTP provider forwards the document/identity to a
configured KYC service. Nothing is auto-marked verified unless a real provider says
so — no fabricated verification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol

import requests


@dataclass
class VerificationResult:
    verified: bool
    status: str
    details: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {"verified": self.verified, "status": self.status, "details": self.details}


@dataclass
class ScreeningResult:
    clear: Optional[bool]
    status: str
    hits: List[Dict] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {"clear": self.clear, "status": self.status, "hits": self.hits}


class VerificationProvider(Protocol):
    name: str
    def verify_document(self, doc: Dict) -> VerificationResult: ...
    def screen(self, identity: Dict) -> ScreeningResult: ...


class ManualVerificationProvider:
    """No automated verification — a human reviews. Never auto-approves."""

    name = "manual"

    def verify_document(self, doc: Dict) -> VerificationResult:
        return VerificationResult(False, "manual_review_required", {"provider": self.name})

    def screen(self, identity: Dict) -> ScreeningResult:
        return ScreeningResult(None, "not_screened", [])


class HttpVerificationProvider:
    """Forwards documents/identities to a configured KYC/OCR/sanctions service."""

    name = "http"

    def __init__(self, verify_url: str, screen_url: Optional[str] = None, timeout: float = 30.0):
        self.verify_url = verify_url
        self.screen_url = screen_url
        self.timeout = timeout

    def verify_document(self, doc: Dict) -> VerificationResult:
        try:
            r = requests.post(self.verify_url, json={"document": doc}, timeout=self.timeout)
            r.raise_for_status()
            body = r.json()
        except (requests.RequestException, ValueError):
            return VerificationResult(False, "provider_unavailable", {"provider": self.name})
        return VerificationResult(bool(body.get("verified")),
                                  body.get("status", "verified" if body.get("verified") else "rejected"),
                                  body.get("details", {}))

    def screen(self, identity: Dict) -> ScreeningResult:
        if not self.screen_url:
            return ScreeningResult(None, "not_screened", [])
        try:
            r = requests.post(self.screen_url, json={"identity": identity}, timeout=self.timeout)
            r.raise_for_status()
            body = r.json()
        except (requests.RequestException, ValueError):
            return ScreeningResult(None, "provider_unavailable", [])
        hits = body.get("hits", [])
        clear = body.get("clear", len(hits) == 0)
        return ScreeningResult(bool(clear), body.get("status", "screened"), hits)


def make_kyc_provider(settings) -> VerificationProvider:
    endpoint = getattr(settings, "kyc_endpoint", None)
    if endpoint:
        return HttpVerificationProvider(endpoint, getattr(settings, "sanctions_endpoint", None))
    return ManualVerificationProvider()
