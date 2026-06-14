"""Escrow, proof-of-delivery and settlement.

Funds are held in escrow on award and released on a verified electronic proof of
delivery (ePOD). Release computes statutory tax/withholding from the admin-managed
tax engine and records a settlement. The payment *gateway* is a pluggable adapter:
the default ``manual`` provider records intent without moving money (no fabricated
transactions); plug a real gateway client to fund/transfer.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Dict, Optional, Protocol

from . import tax_service
from .models import Escrow, ProofOfDelivery, Settlement


class PaymentGateway(Protocol):
    def hold(self, escrow_id: str, amount: float, currency: str) -> Dict: ...
    def transfer(self, escrow_id: str, payee_id: str, amount: float, currency: str) -> Dict: ...


class ManualGateway:
    """Records intent only; an operator/treasury settles out of band."""

    name = "manual"

    def hold(self, escrow_id, amount, currency) -> Dict:
        return {"provider": self.name, "provider_ref": None, "funded": False}

    def transfer(self, escrow_id, payee_id, amount, currency) -> Dict:
        return {"provider": self.name, "provider_ref": None, "transferred": False}


def open_escrow(session, *, shipment_id, load_ref, payer_id, payee_id, amount, currency,
                gateway: Optional[PaymentGateway] = None) -> Escrow:
    gateway = gateway or ManualGateway()
    eid = f"esc_{uuid.uuid4().hex[:10]}"
    held = gateway.hold(eid, amount, currency)
    escrow = Escrow(escrow_id=eid, shipment_id=shipment_id, load_ref=load_ref, payer_id=payer_id,
                    payee_id=payee_id, amount=amount, currency=currency,
                    status="funded" if held.get("funded") else "pending",
                    provider=held.get("provider"), provider_ref=held.get("provider_ref"))
    session.add(escrow)
    session.commit()
    return escrow


def record_epod(session, shipment_id: str, *, recipient_name=None, signature_ref=None,
                photo_refs=None, notes=None, delivered_at=None) -> ProofOfDelivery:
    pod = ProofOfDelivery(shipment_id=shipment_id, recipient_name=recipient_name,
                          signature_ref=signature_ref, photo_refs=photo_refs or [], notes=notes,
                          delivered_at=delivered_at or datetime.utcnow().isoformat())
    session.add(pod)
    session.commit()
    return pod


def has_valid_epod(session, shipment_id: str) -> bool:
    pod = session.query(ProofOfDelivery).filter_by(shipment_id=shipment_id).first()
    return pod is not None and bool(pod.signature_ref or pod.photo_refs)


def release_escrow(session, escrow: Escrow, *, region_code: Optional[str] = None,
                   gateway: Optional[PaymentGateway] = None, require_epod: bool = True) -> Settlement:
    if require_epod and not has_valid_epod(session, escrow.shipment_id):
        raise ValueError("a valid proof of delivery is required before release")
    if escrow.status == "released":
        raise ValueError("escrow already released")
    gateway = gateway or ManualGateway()

    tax = tax_service.compute_for_region(
        session, base_amount=escrow.amount, region_code=region_code,
        category="transport_service", currency_code=escrow.currency)
    net = tax.get("net_payable_to_provider", escrow.amount)
    gateway.transfer(escrow.escrow_id, escrow.payee_id, net, escrow.currency)
    escrow.status = "released"
    escrow.updated_at = datetime.utcnow()
    settlement = Settlement(
        escrow_id=escrow.escrow_id, gross=escrow.amount, tax_total=tax.get("total_add", 0.0),
        withheld=tax.get("total_withheld", 0.0), net_to_payee=net, currency=escrow.currency,
        breakdown=tax)
    session.add(settlement)
    session.commit()
    return settlement
