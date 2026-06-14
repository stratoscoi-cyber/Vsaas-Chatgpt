"""Cargo insurance marketplace: quotes, binding and claims.

Premiums are computed from **operator/insurer-configured rates** (the
``InsuranceRate`` table) — no built-in rates. Quotes can be requested across all
matching insurers; the load owner binds one. Claims are tied to a policy and a
shipment and move through a review workflow.
"""

from __future__ import annotations

import uuid
from typing import Dict, List, Optional

from ..common import currency as currency_mod
from .models import InsuranceClaim, InsurancePolicy, InsuranceRate


def quote(session, *, sum_insured: float, region_code: Optional[str], level: Optional[str] = None,
          currency: str = "USD", locale: str = "en") -> List[Dict]:
    """Return premium quotes from every active matching insurer rate."""
    q = session.query(InsuranceRate).filter(InsuranceRate.active.is_(True))
    rates = [r for r in q.all() if r.region_code in (region_code, "*")]
    if level:
        rates = [r for r in rates if r.level == level]
    quotes = []
    for r in rates:
        premium = max(r.min_premium or 0.0, round(sum_insured * (r.rate_percent or 0) / 100.0, 2))
        quotes.append({
            "rate_code": r.code, "insurer_code": r.insurer_code, "level": r.level,
            "rate_percent": r.rate_percent, "sum_insured": sum_insured, "premium": premium,
            "currency": currency, "premium_formatted": currency_mod.format_amount(premium, currency, locale),
        })
    return sorted(quotes, key=lambda x: x["premium"])


def bind(session, *, load_ref: str, insurer_code: str, level: str, sum_insured: float,
         premium: float, currency: str, shipment_id: Optional[str] = None) -> InsurancePolicy:
    policy = InsurancePolicy(
        policy_id=f"pol_{uuid.uuid4().hex[:10]}", load_ref=load_ref, shipment_id=shipment_id,
        insurer_code=insurer_code, level=level, sum_insured=sum_insured, premium=premium,
        currency=currency, status="bound")
    session.add(policy)
    session.commit()
    return policy


def open_claim(session, *, policy_id: str, reason: str, amount: float,
               shipment_id: Optional[str] = None, evidence: Optional[list] = None) -> InsuranceClaim:
    policy = session.query(InsurancePolicy).filter_by(policy_id=policy_id).first()
    if policy is None:
        raise LookupError("policy not found")
    if policy.status not in ("bound", "claimed"):
        raise ValueError(f"policy not active (status={policy.status})")
    if amount > (policy.sum_insured or 0):
        raise ValueError("claim amount exceeds the sum insured")
    claim = InsuranceClaim(claim_id=f"clm_{uuid.uuid4().hex[:10]}", policy_id=policy_id,
                           shipment_id=shipment_id or policy.shipment_id, reason=reason,
                           amount=amount, status="open", evidence=evidence or [])
    policy.status = "claimed"
    session.add(claim)
    session.commit()
    return claim


def update_claim(session, claim_id: str, status: str) -> InsuranceClaim:
    if status not in ("open", "review", "approved", "rejected", "paid"):
        raise ValueError("invalid claim status")
    claim = session.query(InsuranceClaim).filter_by(claim_id=claim_id).first()
    if claim is None:
        raise LookupError("claim not found")
    claim.status = status
    session.commit()
    return claim
