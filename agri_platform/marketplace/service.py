"""Marketplace business logic: loads, offers, negotiation and AI haggling."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Dict, Optional

from . import negotiation, pricing
from .models import Load, NegotiationMessage, Offer, Vehicle

BIDDER_ROLES = {"transporter", "vehicle_owner", "logistics_manager"}


def new_ref(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# --- pricing -------------------------------------------------------------------

def estimate_for_load(load: Load, *, urgency_factor: float = 1.0, distance_km: Optional[float] = None) -> pricing.RateEstimate:
    return pricing.estimate_rate(
        weight_kg=load.weight_kg or 0,
        dimensions_cm=load.dimensions_cm,
        quantity=load.quantity or 1,
        load_type=load.load_type or "general",
        classifications=load.classifications or [],
        origin=load.origin,
        destination=load.destination,
        distance_km=distance_km if distance_km is not None else load.distance_km,
        urgency_factor=urgency_factor,
        currency=load.currency or "USD",
    )


# --- offers --------------------------------------------------------------------

def place_offer(
    session,
    load: Load,
    *,
    bidder_id: str,
    bidder_role: str,
    price: float,
    vehicle_id: Optional[str] = None,
    eta_hours: Optional[float] = None,
    message: Optional[str] = None,
    negotiation_style: Optional[str] = None,
) -> Offer:
    if load.status not in ("open", "negotiating"):
        raise ValueError(f"load is not accepting offers (status={load.status})")
    if bidder_role not in BIDDER_ROLES:
        raise ValueError(f"invalid bidder_role '{bidder_role}'")
    if vehicle_id:
        _check_vehicle_suitability(session, load, vehicle_id)

    offer = Offer(
        load_ref=load.ref,
        bidder_id=bidder_id,
        bidder_role=bidder_role,
        vehicle_id=vehicle_id,
        price=float(price),
        currency=load.currency or "USD",
        eta_hours=eta_hours,
        message=message,
        negotiation_style=negotiation_style,
        status="proposed",
        round=0,
    )
    session.add(offer)
    load.status = "negotiating"
    session.flush()  # assign offer.id
    _record_message(session, load.ref, offer.id, bidder_id, "bidder", float(price), message or "", negotiation_style)
    session.commit()
    return offer


def _check_vehicle_suitability(session, load: Load, vehicle_id: str) -> None:
    vehicle = session.query(Vehicle).filter_by(vehicle_id=vehicle_id).first()
    if vehicle is None:
        raise ValueError(f"vehicle '{vehicle_id}' not found")
    if vehicle.capacity_kg is not None and load.weight_kg and load.weight_kg > vehicle.capacity_kg:
        raise ValueError("vehicle capacity (kg) is below the load weight")
    features = set(vehicle.features or [])
    classes = set(load.classifications or [])
    if "refrigerated" in classes and "refrigeration" not in features:
        raise ValueError("load requires refrigeration but vehicle lacks it")
    if "hazardous" in classes and "hazmat_certified" not in features:
        raise ValueError("load is hazardous but vehicle is not hazmat certified")
    if "livestock" in classes and "livestock_rated" not in features:
        raise ValueError("load is livestock but vehicle is not livestock rated")


def counter_offer(
    session,
    offer: Offer,
    *,
    price: float,
    author_id: str,
    author_role: str,  # shipper | bidder
    message: Optional[str] = None,
    negotiation_style: Optional[str] = None,
) -> Offer:
    if offer.status in ("accepted", "rejected", "withdrawn"):
        raise ValueError(f"offer is closed (status={offer.status})")
    offer.price = float(price)
    offer.round = (offer.round or 0) + 1
    offer.status = "countered"
    offer.updated_at = datetime.utcnow()
    if negotiation_style:
        offer.negotiation_style = negotiation_style
    _record_message(session, offer.load_ref, offer.id, author_id, author_role, float(price), message or "", negotiation_style)
    session.commit()
    return offer


def accept_offer(session, load: Load, offer: Offer) -> Offer:
    if offer.status in ("rejected", "withdrawn"):
        raise ValueError("cannot accept a closed offer")
    offer.status = "accepted"
    offer.updated_at = datetime.utcnow()
    load.status = "awarded"
    load.awarded_offer_id = offer.id
    # Reject the remaining open offers on this load.
    for other in session.query(Offer).filter(Offer.load_ref == load.ref, Offer.id != offer.id).all():
        if other.status in ("proposed", "countered"):
            other.status = "rejected"
            other.updated_at = datetime.utcnow()
    session.commit()
    return offer


def reject_offer(session, offer: Offer) -> Offer:
    offer.status = "rejected"
    offer.updated_at = datetime.utcnow()
    session.commit()
    return offer


def _record_message(session, load_ref, offer_id, author_id, author_role, price, body, style) -> NegotiationMessage:
    msg = NegotiationMessage(
        load_ref=load_ref, offer_id=offer_id, author_id=author_id, author_role=author_role,
        price=price, body=body, negotiation_style=style,
    )
    session.add(msg)
    return msg


# --- AI haggling ---------------------------------------------------------------

def haggle(
    session,
    load: Load,
    offer: Offer,
    *,
    as_role: str,                # shipper | bidder
    target_price: Optional[float] = None,
    style: Optional[str] = None,
    custom_profile: Optional[Dict] = None,
    composer: Optional[negotiation.MessageComposer] = None,
    counterparty_name: Optional[str] = None,
    record: bool = True,
) -> Dict:
    """Produce an AI haggling suggestion (action + counter price + message).

    The decision is deterministic; the message is phrased per the chosen
    negotiation-style profile. When ``record`` is true the suggestion is appended
    to the negotiation thread as an ``assistant`` message.
    """
    if as_role not in ("shipper", "bidder"):
        raise ValueError("as_role must be 'shipper' or 'bidder'")

    estimate = estimate_for_load(load)
    if target_price is None:
        if as_role == "shipper":
            target_price = float(load.budget) if load.budget else estimate.low
        else:
            target_price = estimate.high

    profile = negotiation.build_profile(custom_profile) if custom_profile else negotiation.get_profile(style or offer.negotiation_style)
    move = negotiation.decide_move(
        my_target=target_price,
        their_price=offer.price,
        role=as_role,
        round_index=(offer.round or 0) + 1,
        profile=profile,
    )
    composer = composer or negotiation.TemplateComposer()
    context = {
        "counterparty_name": counterparty_name,
        "load_title": load.title,
        "fair_rate": estimate.recommended,
    }
    message = composer.compose(profile, as_role, move, load.currency or "USD", context)

    if record:
        _record_message(session, load.ref, offer.id, "assistant", "assistant", move.counter_price, message, profile.key)
        session.commit()

    return {
        "move": move.to_dict(),
        "message": message,
        "target_price": round(target_price, 2),
        "their_price": offer.price,
        "fair_rate": estimate.to_dict(),
        "profile": profile.to_dict(),
    }
