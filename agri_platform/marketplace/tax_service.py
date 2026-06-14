"""LGaaS tax orchestration: load admin rules for a region and compute taxes.

All rates come from the operator-managed :class:`TaxRule` table — nothing is
hardcoded. The actual maths lives in :mod:`agri_platform.common.tax`.
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional

from sqlalchemy import or_

from ..common import currency, tax
from .models import TaxRule


def rules_for_region(session, region_code: Optional[str]) -> List[dict]:
    """Active rules for a region (its own rules plus global ``*`` rules)."""
    q = session.query(TaxRule).filter(TaxRule.active.is_(True))
    if region_code:
        q = q.filter(or_(TaxRule.region_code == region_code, TaxRule.region_code == "*"))
    else:
        q = q.filter(TaxRule.region_code == "*")
    return [r.to_rule() for r in q.all()]


def compute_for_region(
    session,
    *,
    base_amount: float,
    region_code: Optional[str],
    category: Optional[str] = "transport_service",
    currency_code: str = "USD",
    locale: str = "en",
    on_date: Optional[date] = None,
) -> dict:
    rules = rules_for_region(session, region_code)
    result = tax.compute_taxes(
        base_amount, rules, category=category, currency=currency_code, on_date=on_date
    )
    result["region_code"] = region_code
    result["formatted"] = {
        key: currency.format_amount(result[key], currency_code, locale)
        for key in ("base_amount", "total_add", "total_withheld", "gross_total", "net_payable_to_provider")
    }
    for line in result["lines"]:
        line["amount_formatted"] = currency.format_amount(line["amount"], currency_code, locale)
    return result
