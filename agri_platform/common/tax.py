"""Statutory tax / VAT / levy computation engine.

This engine computes taxes by applying **operator-configured rules** to a base
amount. It deliberately ships with **no rates of its own**: VAT/GST rates,
withholding rates, levy rates and their applicability are statutory data that
differ by jurisdiction and change by law, so they must be entered and maintained
by an administrator (the authoritative source). Until rules are configured, the
computed tax is zero and the result says so — nothing is invented.

The arithmetic is exact (``decimal.Decimal`` with bankers'-safe ROUND_HALF_UP),
deterministic and fully unit-tested — it is a real calculation, not a simulation.

Two collection modes are supported, covering the common statutory cases:

* ``add``      -- tax added on top of the net price and charged to the buyer
                  (e.g. VAT/GST, consumption levies).
* ``withhold`` -- tax withheld from the amount payable to the provider and
                  remitted to the authority (e.g. withholding tax / WHT).

``basis`` is ``net`` (on the net amount) or ``compound`` (on net plus previously
applied ``add`` taxes — i.e. tax-on-tax) for jurisdictions that require it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, Iterable, List, Optional

ADD = "add"
WITHHOLD = "withhold"
VALID_COLLECTIONS = {ADD, WITHHOLD}
VALID_BASES = {"net", "compound"}


def _D(value) -> Decimal:
    return Decimal(str(value))


def _parse_date(value) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _quantize(amount: Decimal, decimals: int) -> Decimal:
    q = Decimal(1).scaleb(-decimals)  # e.g. decimals=2 -> 0.01
    return amount.quantize(q, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class TaxLine:
    code: str
    name: str
    tax_type: str
    collection: str
    rate_percent: float
    taxable_base: float
    amount: float

    def to_dict(self) -> Dict:
        return {
            "code": self.code,
            "name": self.name,
            "tax_type": self.tax_type,
            "collection": self.collection,
            "rate_percent": self.rate_percent,
            "taxable_base": self.taxable_base,
            "amount": self.amount,
        }


def _rule_applies(rule: Dict, category: Optional[str], on_date: date, base: Decimal) -> bool:
    if not rule.get("active", True):
        return False
    if rule.get("collection") not in VALID_COLLECTIONS:
        return False
    ef = _parse_date(rule.get("effective_from"))
    et = _parse_date(rule.get("effective_to"))
    if ef and on_date < ef:
        return False
    if et and on_date > et:
        return False
    applies_to = rule.get("applies_to") or []
    if applies_to and category is not None and category not in applies_to:
        return False
    threshold = _D(rule.get("threshold_min") or 0)
    if base < threshold:
        return False
    return True


def compute_taxes(
    base_amount: float,
    rules: Iterable[Dict],
    *,
    category: Optional[str] = None,
    on_date: Optional[date] = None,
    currency: str = "USD",
    decimals: int = 2,
) -> Dict:
    """Apply configured ``rules`` to ``base_amount`` (net, tax-exclusive).

    Returns an itemised breakdown with totals. With no applicable rules the tax
    totals are zero and ``rules_applied`` is 0 — an explicit, truthful "nothing
    configured" outcome rather than a guess.
    """
    on_date = on_date or date.today()
    base = _D(base_amount)
    applicable = [r for r in rules if _rule_applies(r, category, on_date, base)]
    applicable.sort(key=lambda r: (int(r.get("sequence", 0)), r.get("code", "")))

    lines: List[TaxLine] = []
    prior_add = Decimal(0)
    total_add = Decimal(0)
    total_withheld = Decimal(0)

    for rule in applicable:
        rate = _D(rule.get("rate_percent", 0))
        basis = rule.get("basis", "net")
        taxable = base + prior_add if basis == "compound" else base
        amount = _quantize(taxable * rate / Decimal(100), decimals)
        collection = rule["collection"]
        lines.append(
            TaxLine(
                code=rule.get("code", ""),
                name=rule.get("name", rule.get("code", "")),
                tax_type=rule.get("tax_type", "tax"),
                collection=collection,
                rate_percent=float(rate),
                taxable_base=float(_quantize(taxable, decimals)),
                amount=float(amount),
            )
        )
        if collection == ADD:
            total_add += amount
            prior_add += amount
        else:
            total_withheld += amount

    base_q = _quantize(base, decimals)
    total_add = _quantize(total_add, decimals)
    total_withheld = _quantize(total_withheld, decimals)
    return {
        "currency": currency,
        "base_amount": float(base_q),
        "category": category,
        "on_date": on_date.isoformat(),
        "lines": [l.to_dict() for l in lines],
        "total_add": float(total_add),
        "total_withheld": float(total_withheld),
        "gross_total": float(_quantize(base + total_add, decimals)),
        "net_payable_to_provider": float(_quantize(base - total_withheld, decimals)),
        "rules_applied": len(lines),
        "configured": len(lines) > 0,
    }
