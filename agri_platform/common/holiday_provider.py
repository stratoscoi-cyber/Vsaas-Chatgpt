"""Pluggable public-holiday data sources.

Two real sources, no fabrication:

* the operator's admin-managed :class:`Holiday` table (authoritative overrides),
  and
* the maintained `python-holidays <https://pypi.org/project/holidays/>`_ package,
  which ships real, community-maintained national holiday calendars (including
  movable/observed dates such as Eid).

``make_holiday_provider`` selects the source by mode. ``library`` layers the
maintained calendars under the admin table (admin entries always win on a date
clash). If the library is not installed, the provider degrades to the admin-only
source rather than guessing.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Dict, Protocol

logger = logging.getLogger(__name__)


class HolidayProvider(Protocol):
    def dates(self, region_code: str, year: int) -> Dict[date, str]: ...


class NullHolidayProvider:
    """No external dates — only the admin-managed table is used."""

    def dates(self, region_code: str, year: int) -> Dict[date, str]:
        return {}


class LibraryHolidayProvider:
    """Maintained national calendars from the ``holidays`` package."""

    def __init__(self):
        import holidays  # imported here so the dependency stays optional

        self._holidays = holidays

    def dates(self, region_code: str, year: int) -> Dict[date, str]:
        if not region_code or region_code == "ZZ":
            return {}
        try:
            cal = self._holidays.country_holidays(region_code, years=year)
            return {d: name for d, name in cal.items()}
        except (KeyError, NotImplementedError):
            return {}  # country not covered by the library
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("holiday library lookup failed for %s %s: %s", region_code, year, exc)
            return {}


def make_holiday_provider(mode: str = "admin") -> HolidayProvider:
    """``admin`` -> table only; ``library`` -> maintained calendars + admin."""
    if mode == "library":
        try:
            return LibraryHolidayProvider()
        except Exception:  # library not installed
            logger.warning("HOLIDAY_PROVIDER=library but the 'holidays' package is unavailable; using admin-only")
    return NullHolidayProvider()
