"""Background monitoring: periodically scan registered farms for hazards.

``scan_farms`` is a pure-ish function (it takes explicit collaborators) so it can
be unit-tested without a scheduler. :class:`MonitorScheduler` wraps it in an
APScheduler background job for production use.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Callable, List, Optional

from . import service
from .models import Farm, WeatherAlert
from .notifications import Notifier, dispatch_alert

logger = logging.getLogger(__name__)


def scan_farms(
    session,
    weather_client,
    notifier: Optional[Notifier] = None,
    today: Optional[str] = None,
) -> List[WeatherAlert]:
    """Evaluate weather + soil hazards for every farm with coordinates.

    Persists any triggered alerts and dispatches notifications. Returns the list
    of created alerts.
    """
    day = today or date.today().isoformat()
    created: List[WeatherAlert] = []
    farms = session.query(Farm).all()
    for farm in farms:
        coords = farm.coordinates or {}
        lat, lon = coords.get("lat"), coords.get("lon")
        if lat is None or lon is None:
            continue
        try:
            wx_hourly = weather_client.hourly(
                lat, lon,
                ["precipitation_probability", "wind_speed_10m", "soil_moisture_1_to_3cm"],
                day, day,
            ).get("hourly", {})
        except Exception as exc:  # pragma: no cover - network failure path
            logger.warning("weather scan failed for farm %s: %s", farm.farm_id, exc)
            continue

        specs = []
        weather_spec = service.evaluate_weather_hazard(
            wx_hourly.get("precipitation_probability", []),
            wx_hourly.get("wind_speed_10m", []),
        )
        if weather_spec:
            specs.append(weather_spec)
        soil_spec = service.evaluate_soil_condition(wx_hourly.get("soil_moisture_1_to_3cm", []))
        if soil_spec:
            specs.append(soil_spec)

        for spec in specs:
            alert = service.create_alert(session, farm.farm_id, spec, {"lat": lat, "lon": lon})
            created.append(alert)
            if notifier is not None:
                dispatch_alert(session, notifier, alert)
    return created


class MonitorScheduler:
    """Runs :func:`scan_farms` on a fixed interval using APScheduler."""

    def __init__(self, scan_callable: Callable[[], None], interval_minutes: int = 30):
        self._scan = scan_callable
        self._interval = interval_minutes
        self._scheduler = None

    def start(self) -> None:
        from apscheduler.schedulers.background import BackgroundScheduler

        self._scheduler = BackgroundScheduler(daemon=True)
        self._scheduler.add_job(
            self._safe_scan, "interval", minutes=self._interval, id="farm_scan", replace_existing=True
        )
        self._scheduler.start()
        logger.info("monitor scheduler started (every %s min)", self._interval)

    def _safe_scan(self) -> None:
        try:
            self._scan()
        except Exception:  # pragma: no cover - defensive
            logger.exception("scheduled farm scan failed")

    def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
