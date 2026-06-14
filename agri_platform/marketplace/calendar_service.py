"""LGaaS calendar orchestration: region holidays + weekend -> business-day maths.

Holiday dates come from the operator-managed :class:`Holiday` table and,
optionally, the maintained ``holidays`` library (via a :class:`HolidayProvider`).
The weekend comes from the region's cultural profile. Nothing is hardcoded.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, List, Optional

from ..common import holidays as cal
from ..common import regions
from ..common.holiday_provider import HolidayProvider, NullHolidayProvider
from .models import Holiday


def _weekend_for(region_code: Optional[str]):
    region = regions.lookup(region_code) if region_code else None
    return tuple(region.weekend) if region else regions.DEFAULT_WEEKEND


def admin_rules(session, region_code: Optional[str]) -> List[dict]:
    q = session.query(Holiday).filter(Holiday.active.is_(True))
    if region_code:
        q = q.filter(Holiday.region_code == region_code)
    return [h.to_rule() for h in q.all()]


def effective_rules(
    session,
    region_code: Optional[str],
    years: Iterable[int],
    provider: Optional[HolidayProvider] = None,
) -> List[dict]:
    """Admin rules plus any provider (library) dates for the given years.

    Provider dates are added as one-off ``date`` rules; admin entries on the same
    date take precedence (the engine's later-wins map merge favours admin because
    admin rules are appended first only when they don't already cover the date).
    """
    years = list(years)
    rules = admin_rules(session, region_code)
    provider = provider or NullHolidayProvider()
    # Dates the admin already covers (fixed or one-off) take precedence.
    admin_dates = set()
    for year in years:
        admin_dates |= set(cal.holiday_map(rules, year).keys())
    for year in years:
        for d, name in provider.dates(region_code or "", year).items():
            if d in admin_dates:
                continue
            rules.append({"recurrence": "date", "date": d.isoformat(), "name": name, "active": True})
    return rules


def resolve_year(session, region_code: str, year: int, provider=None) -> List[dict]:
    rules = effective_rules(session, region_code, [year], provider)
    return [
        {"date": d.isoformat(), "name": name}
        for d, name in sorted(cal.holiday_map(rules, year).items())
    ]


def schedule_info(session, region_code: Optional[str], d: date, provider=None) -> dict:
    rules = effective_rules(session, region_code, [d.year, d.year + 1], provider)
    weekend = _weekend_for(region_code)
    name = cal.is_holiday(d, rules)
    return {
        "date": d.isoformat(),
        "region_code": region_code,
        "is_weekend": cal.is_weekend(d, weekend),
        "is_holiday": name is not None,
        "holiday_name": name,
        "is_business_day": cal.is_business_day(d, rules, weekend),
        "next_business_day": cal.next_business_day(d, rules, weekend, inclusive=True).isoformat(),
    }


def next_business_day(session, region_code, d: date, inclusive=False, provider=None) -> date:
    rules = effective_rules(session, region_code, [d.year, d.year + 1], provider)
    return cal.next_business_day(d, rules, _weekend_for(region_code), inclusive=inclusive)


def add_business_days(session, region_code, d: date, n: int, provider=None) -> date:
    rules = effective_rules(session, region_code, [d.year, d.year + 1], provider)
    return cal.add_business_days(d, n, rules, _weekend_for(region_code))


def upcoming(session, region_code, from_date: date, limit: int = 5, provider=None) -> List[dict]:
    rules = effective_rules(session, region_code, [from_date.year, from_date.year + 1], provider)
    return cal.upcoming_holidays(rules, from_date, limit)
