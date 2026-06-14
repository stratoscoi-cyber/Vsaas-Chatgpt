"""Public-holiday calendar engine.

Like the tax engine, this computes from **operator-supplied data**, not built-in
dates: public holidays vary by jurisdiction, and movable/observed holidays (e.g.
those on lunar calendars) are gazetted each year, so an administrator enters the
real dates. The engine only does calendar arithmetic — resolving recurring
fixed-date holidays for a given year, and computing business days around weekends
and holidays. It invents no dates.

Holiday rule shapes (dicts):
* ``recurrence="fixed"`` with ``month`` and ``day`` -> recurs every year.
* ``recurrence="date"`` with ``date`` (ISO ``YYYY-MM-DD``) -> a single observed
  date (used for movable/gazetted holidays the admin enters per year).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

DEFAULT_WEEKEND: Tuple[int, ...] = (5, 6)  # Sat, Sun


def _parse_date(value) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def holiday_map(rules: Iterable[Dict], year: int) -> Dict[date, str]:
    """Resolve active rules into a ``{date: name}`` map for ``year``."""
    result: Dict[date, str] = {}
    for rule in rules:
        if not rule.get("active", True):
            continue
        name = rule.get("name", rule.get("code", "holiday"))
        if rule.get("recurrence") == "fixed":
            month, day = rule.get("month"), rule.get("day")
            if not month or not day:
                continue
            try:
                result[date(year, int(month), int(day))] = name
            except ValueError:
                continue  # e.g. Feb 29 in a non-leap year
        else:  # one-off observed date
            d = _parse_date(rule.get("date"))
            if d is not None and d.year == year:
                result[d] = name
    return result


def is_weekend(d: date, weekend: Iterable[int] = DEFAULT_WEEKEND) -> bool:
    return d.weekday() in set(weekend)


def is_holiday(d: date, rules: Iterable[Dict]) -> Optional[str]:
    """Return the holiday name if ``d`` is a configured holiday, else ``None``."""
    return holiday_map(list(rules), d.year).get(d)


def is_business_day(d: date, rules: Iterable[Dict], weekend: Iterable[int] = DEFAULT_WEEKEND) -> bool:
    rules = list(rules)
    return not is_weekend(d, weekend) and is_holiday(d, rules) is None


def next_business_day(
    d: date, rules: Iterable[Dict], weekend: Iterable[int] = DEFAULT_WEEKEND, inclusive: bool = False
) -> date:
    """The next business day on/after ``d`` (strictly after unless ``inclusive``)."""
    rules = list(rules)
    candidate = d if inclusive else d + timedelta(days=1)
    while not is_business_day(candidate, rules, weekend):
        candidate += timedelta(days=1)
    return candidate


def add_business_days(
    d: date, n: int, rules: Iterable[Dict], weekend: Iterable[int] = DEFAULT_WEEKEND
) -> date:
    """Advance ``n`` business days from ``d`` (n>=0)."""
    rules = list(rules)
    candidate = d
    remaining = n
    while remaining > 0:
        candidate += timedelta(days=1)
        if is_business_day(candidate, rules, weekend):
            remaining -= 1
    return candidate


def business_days_between(
    start: date, end: date, rules: Iterable[Dict], weekend: Iterable[int] = DEFAULT_WEEKEND
) -> int:
    """Count business days in ``(start, end]`` (excludes ``start``, includes ``end``)."""
    rules = list(rules)
    if end <= start:
        return 0
    count = 0
    candidate = start + timedelta(days=1)
    while candidate <= end:
        if is_business_day(candidate, rules, weekend):
            count += 1
        candidate += timedelta(days=1)
    return count


def upcoming_holidays(rules: Iterable[Dict], from_date: date, limit: int = 5) -> List[Dict]:
    """The next ``limit`` holidays on/after ``from_date`` (searches up to 2 years)."""
    rules = list(rules)
    found: List[Tuple[date, str]] = []
    for year in (from_date.year, from_date.year + 1):
        for d, name in holiday_map(rules, year).items():
            if d >= from_date:
                found.append((d, name))
    found.sort(key=lambda x: x[0])
    return [{"date": d.isoformat(), "name": name} for d, name in found[:limit]]
