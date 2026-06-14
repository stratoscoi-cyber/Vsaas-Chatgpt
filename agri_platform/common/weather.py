"""Thin client for the Open-Meteo forecast API.

Open-Meteo is free for non-commercial use and needs no API key. The client is
deliberately small and side-effect free so it can be swapped for a fake in
tests (see :class:`StaticWeatherClient`).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Protocol

import requests

DEFAULT_BASE_URL = "https://api.open-meteo.com/v1/forecast"


class WeatherError(RuntimeError):
    """Raised when the upstream weather provider fails or returns an error."""


class WeatherClient(Protocol):
    """Interface the services depend on; keeps them decoupled from HTTP."""

    def hourly(
        self,
        lat: float,
        lon: float,
        variables: List[str],
        start_date: str,
        end_date: str,
    ) -> Dict: ...

    def daily(
        self,
        lat: float,
        lon: float,
        variables: List[str],
        start_date: str,
        end_date: str,
    ) -> Dict: ...


class OpenMeteoClient:
    """Live client backed by ``requests``."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = 15.0):
        self.base_url = base_url
        self.timeout = timeout

    def _get(self, params: Dict) -> Dict:
        try:
            resp = requests.get(self.base_url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:  # network / HTTP error
            raise WeatherError(f"weather request failed: {exc}") from exc
        except ValueError as exc:  # invalid JSON
            raise WeatherError(f"invalid weather response: {exc}") from exc
        if isinstance(data, dict) and data.get("error"):
            raise WeatherError(str(data.get("reason", "unknown weather API error")))
        return data

    def hourly(self, lat, lon, variables, start_date, end_date) -> Dict:
        return self._get(
            {
                "latitude": lat,
                "longitude": lon,
                "hourly": ",".join(variables),
                "start_date": start_date,
                "end_date": end_date,
            }
        )

    def daily(self, lat, lon, variables, start_date, end_date) -> Dict:
        return self._get(
            {
                "latitude": lat,
                "longitude": lon,
                "daily": ",".join(variables),
                "start_date": start_date,
                "end_date": end_date,
            }
        )


class StaticWeatherClient:
    """In-memory client for tests and offline demos.

    ``hourly_values``/``daily_values`` map a variable name to the list returned
    for every request. ``times`` is reused for all responses.
    """

    def __init__(
        self,
        hourly_values: Optional[Dict[str, List]] = None,
        daily_values: Optional[Dict[str, List]] = None,
        times: Optional[List[str]] = None,
    ):
        self.hourly_values = hourly_values or {}
        self.daily_values = daily_values or {}
        self.times = times or []

    def hourly(self, lat, lon, variables, start_date, end_date) -> Dict:
        return {
            "latitude": lat,
            "longitude": lon,
            "hourly": {"time": self.times, **{v: self.hourly_values.get(v, []) for v in variables}},
        }

    def daily(self, lat, lon, variables, start_date, end_date) -> Dict:
        return {
            "latitude": lat,
            "longitude": lon,
            "daily": {"time": self.times, **{v: self.daily_values.get(v, []) for v in variables}},
        }
