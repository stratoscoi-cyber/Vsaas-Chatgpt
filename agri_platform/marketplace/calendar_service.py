"""LGaaS calendar orchestration: region holidays + weekend -> business-day maths.

Holiday dates come from the operator-managed :class:`Holiday` table; the weekend
comes from the region's cultural profile. Nothing is hardcoded here.
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional

from ..common import holidays as cal
from ..common import regions
from .models import Holiday


def _weekend_for(region_code: Optional[str]):
    region = regions.lookup(region_code) if region_code else None
    return tuple(region.weekend) if region else regions.DEFAULT_WEEKEND


def rules_for_region(session, region_code: Optional[str]) -> List[dict]:
    q = session.query(Holiday).filter(Holiday.active.is_(True))
    if region_code:
        q = q.filter(Holiday.region_code == region_code)
    return [h.to_rule() for h in q.all()]


def resolve_year(session, region_code: str, year: int) -> List[dict]:
    rules = rules_for_region(session, region_code)
    return [
        {"date": d.isoformat(), "name": name}
        for d, name in sorted(cal.holiday_map(rules, year).items())
    ]


def schedule_info(session, region_code: Optional[str], d: date) -> dict:
    rules = rules_for_region(session, region_code)
    weekend = _weekend_for(region_code)
    name = cal.is_holiday(d, rules)
    business = cal.is_business_day(d, rules, weekend)
    return {
        "date": d.isoformat(),
        "region_code": region_code,
        "is_weekend": cal.is_weekend(d, weekend),
        "is_holiday": name is not None,
        "holiday_name": name,
        "is_business_day": business,
        "next_business_day": cal.next_business_day(d, rules, weekend, inclusive=True).isoformat(),
    }


def next_business_day(session, region_code: Optional[str], d: date, inclusive: bool = False) -> date:
    rules = rules_for_region(session, region_code)
    return cal.next_business_day(d, rules, _weekend_for(region_code), inclusive=inclusive)


def add_business_days(session, region_code: Optional[str], d: date, n: int) -> date:
    rules = rules_for_region(session, region_code)
    return cal.add_business_days(d, n, rules, _weekend_for(region_code))


def upcoming(session, region_code: Optional[str], from_date: date, limit: int = 5) -> List[dict]:
    return cal.upcoming_holidays(rules_for_region(session, region_code), from_date, limit)
