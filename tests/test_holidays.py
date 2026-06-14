"""Tests for the public-holiday / business-day calendar engine and region nuance."""

from datetime import date

from agri_platform.common import holidays as cal
from agri_platform.common import regions


# A configured New Year's Day (fixed) and a one-off observed date.
RULES = [
    {"code": "NY", "name": "New Year's Day", "recurrence": "fixed", "month": 1, "day": 1, "active": True},
    {"code": "EID", "name": "Observed Holiday", "recurrence": "date", "date": "2026-03-20", "active": True},
    {"code": "OLD", "name": "Disabled", "recurrence": "fixed", "month": 6, "day": 1, "active": False},
]


def test_holiday_map_resolves_fixed_and_dated():
    m = cal.holiday_map(RULES, 2026)
    assert m[date(2026, 1, 1)] == "New Year's Day"
    assert m[date(2026, 3, 20)] == "Observed Holiday"
    assert date(2026, 6, 1) not in m  # inactive rule excluded


def test_one_off_only_in_its_year():
    assert date(2026, 3, 20) in cal.holiday_map(RULES, 2026)
    assert date(2026, 3, 20) not in cal.holiday_map(RULES, 2027)


def test_is_business_day_weekend_and_holiday():
    assert cal.is_business_day(date(2026, 1, 1), RULES) is False   # holiday (Thu)
    assert cal.is_business_day(date(2026, 1, 3), RULES) is False   # Saturday
    assert cal.is_business_day(date(2026, 1, 2), RULES) is True    # Friday, normal


def test_next_business_day_skips_holiday_and_weekend():
    # 2026-01-01 is Thu holiday; 2 is Fri (business) -> next after Jan 1 is Jan 2.
    assert cal.next_business_day(date(2026, 1, 1), RULES) == date(2026, 1, 2)
    # From Fri Jan 2 -> skip Sat/Sun -> Mon Jan 5
    assert cal.next_business_day(date(2026, 1, 2), RULES) == date(2026, 1, 5)


def test_add_business_days():
    # From Mon 2026-01-05, +5 business days -> Mon 2026-01-12
    assert cal.add_business_days(date(2026, 1, 5), 5, RULES) == date(2026, 1, 12)


def test_friday_saturday_weekend_override():
    # With a Fri/Sat weekend, Friday is not a business day but Sunday is.
    we = (4, 5)
    assert cal.is_business_day(date(2026, 1, 2), RULES, we) is False  # Friday
    assert cal.is_business_day(date(2026, 1, 4), RULES, we) is True   # Sunday


def test_business_days_between():
    # Mon 5 -> Fri 9 inclusive of end, exclusive of start = 4 business days
    assert cal.business_days_between(date(2026, 1, 5), date(2026, 1, 9), RULES) == 4


def test_upcoming_holidays_sorted():
    up = cal.upcoming_holidays(RULES, date(2026, 1, 2), limit=5)
    dates = [h["date"] for h in up]
    assert dates == sorted(dates)
    assert "2026-03-20" in dates


# --- region cultural nuance ----------------------------------------------------

def test_region_default_weekend_and_style():
    ng = regions.lookup("NG")
    assert ng.weekend == (5, 6)
    assert ng.default_negotiation_style == "market_bargaining"


def test_egypt_friday_saturday_weekend():
    assert regions.lookup("EG").weekend == (4, 5)


def test_region_to_dict_has_cultural_fields():
    d = regions.lookup("KE").to_dict()
    assert "weekend" in d and "default_negotiation_style" in d
