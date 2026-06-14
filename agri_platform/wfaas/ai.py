"""Client for an external drone-imagery inference service.

This is an integration boundary, not a model: it forwards captured imagery
references to a configured AI endpoint (e.g. a crop-health / disease-detection
model served separately) and returns whatever that service responds with. There
is no built-in/fabricated analysis -- if no endpoint is configured the caller
gets a clear "unavailable" signal.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Protocol

import requests


class InferenceError(RuntimeError):
    pass


class InferenceClient(Protocol):
    def analyze(self, mission_id: str, image_refs: List[str], context: Optional[Dict] = None) -> Dict: ...


class HttpInferenceClient:
    """POSTs an analysis request to a remote inference endpoint."""

    def __init__(self, endpoint_url: str, timeout: float = 30.0):
        self.endpoint_url = endpoint_url
        self.timeout = timeout

    def analyze(self, mission_id, image_refs, context=None) -> Dict:
        try:
            resp = requests.post(
                self.endpoint_url,
                json={"mission_id": mission_id, "images": image_refs, "context": context or {}},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise InferenceError(f"inference request failed: {exc}") from exc
        except ValueError as exc:
            raise InferenceError(f"invalid inference response: {exc}") from exc


class UnavailableInferenceClient:
    """Placeholder used when no inference endpoint is configured."""

    def analyze(self, mission_id, image_refs, context=None) -> Dict:
        raise InferenceError("no drone inference endpoint configured (set DRONE_AI_ENDPOINT)")


def make_inference_client(endpoint_url: Optional[str]) -> InferenceClient:
    return HttpInferenceClient(endpoint_url) if endpoint_url else UnavailableInferenceClient()
