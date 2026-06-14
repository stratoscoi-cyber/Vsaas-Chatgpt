"""Africa-centric region registry and point-based region detection.

The platform defaults to African markets. Each region carries the metadata the
services need to localize automatically: spoken languages (primary first),
settlement currency, timezone and an approximate geographic bounding box used to
infer the region from coordinates.

Bounding boxes are deliberately approximate (public country extents) and are used
for a best-effort "which country is this point in" lookup; for authoritative
administrative boundaries, swap in a PostGIS point-in-polygon query against an
admin-0 layer.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import List, Optional, Tuple

# Monday=0 .. Sunday=6. Default rest days are Saturday & Sunday.
DEFAULT_WEEKEND: Tuple[int, ...] = (5, 6)


@dataclass(frozen=True)
class Region:
    code: str            # ISO-3166 alpha-2
    name: str
    languages: Tuple[str, ...]  # primary first (ISO-639-1 where available)
    currency: str        # ISO-4217
    timezone: str
    bbox: Tuple[float, float, float, float]  # (lon_min, lat_min, lon_max, lat_max)
    weekend: Tuple[int, ...] = DEFAULT_WEEKEND
    # Default negotiation-style preset (a configurable convention, not an
    # assumption about individuals); see marketplace.negotiation.PROFILES.
    default_negotiation_style: str = "relationship_first"

    @property
    def primary_language(self) -> str:
        return self.languages[0]

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "languages": list(self.languages),
            "primary_language": self.primary_language,
            "currency": self.currency,
            "timezone": self.timezone,
            "bbox": list(self.bbox),
            "weekend": list(self.weekend),
            "default_negotiation_style": self.default_negotiation_style,
        }


# A pan-African default for points that fall outside the known boxes. Currency is
# USD as a neutral cross-border settlement unit.
DEFAULT_REGION = Region(
    code="ZZ", name="Africa (unspecified)", languages=("en", "fr"),
    currency="USD", timezone="UTC", bbox=(-25.5, -35.0, 63.5, 37.5),
)

# Curated set of African markets (approximate extents).
REGIONS: Tuple[Region, ...] = (
    Region("NG", "Nigeria", ("en", "ha", "yo"), "NGN", "Africa/Lagos", (2.7, 4.3, 14.7, 13.9)),
    Region("GH", "Ghana", ("en",), "GHS", "Africa/Accra", (-3.3, 4.7, 1.2, 11.2)),
    Region("SN", "Senegal", ("fr",), "XOF", "Africa/Dakar", (-17.5, 12.3, -11.3, 16.7)),
    Region("CI", "Côte d'Ivoire", ("fr",), "XOF", "Africa/Abidjan", (-8.6, 4.3, -2.5, 10.7)),
    Region("ML", "Mali", ("fr",), "XOF", "Africa/Bamako", (-12.3, 10.1, 4.3, 25.0)),
    Region("NE", "Niger", ("fr", "ha"), "XOF", "Africa/Niamey", (0.2, 11.7, 16.0, 23.5)),
    Region("CM", "Cameroon", ("fr", "en"), "XAF", "Africa/Douala", (8.5, 1.7, 16.2, 13.1)),
    Region("CD", "DR Congo", ("fr", "sw"), "CDF", "Africa/Kinshasa", (12.2, -13.5, 31.3, 5.4)),
    Region("KE", "Kenya", ("sw", "en"), "KES", "Africa/Nairobi", (33.9, -4.7, 41.9, 5.0)),
    Region("TZ", "Tanzania", ("sw", "en"), "TZS", "Africa/Dar_es_Salaam", (29.3, -11.7, 40.4, -1.0)),
    Region("UG", "Uganda", ("en", "sw"), "UGX", "Africa/Kampala", (29.5, -1.5, 35.0, 4.2)),
    Region("RW", "Rwanda", ("en", "fr"), "RWF", "Africa/Kigali", (28.8, -2.9, 30.9, -1.0)),
    Region("ET", "Ethiopia", ("am", "en"), "ETB", "Africa/Addis_Ababa", (33.0, 3.4, 48.0, 14.9)),
    Region("ZA", "South Africa", ("en",), "ZAR", "Africa/Johannesburg", (16.5, -34.8, 32.9, -22.1)),
    Region("ZM", "Zambia", ("en",), "ZMW", "Africa/Lusaka", (22.0, -18.1, 33.7, -8.2)),
    Region("ZW", "Zimbabwe", ("en",), "USD", "Africa/Harare", (25.2, -22.5, 33.1, -15.6)),
    Region("AO", "Angola", ("pt",), "AOA", "Africa/Luanda", (11.6, -18.0, 24.1, -4.4)),
    Region("MZ", "Mozambique", ("pt",), "MZN", "Africa/Maputo", (30.2, -26.9, 40.8, -10.5)),
    Region("EG", "Egypt", ("ar", "en"), "EGP", "Africa/Cairo", (25.0, 22.0, 36.0, 31.7)),
    Region("MA", "Morocco", ("ar", "fr"), "MAD", "Africa/Casablanca", (-13.2, 27.7, -1.0, 35.9)),
    Region("TN", "Tunisia", ("ar", "fr"), "TND", "Africa/Tunis", (7.5, 30.2, 11.6, 37.6)),
)

# Cultural-nuance overrides: rest days and a default negotiation-style preset.
# Friday–Saturday weekends are used where that is the official rest period.
_OVERRIDES = {
    "EG": {"weekend": (4, 5), "default_negotiation_style": "high_context_formal"},
    "MA": {"default_negotiation_style": "high_context_formal"},
    "TN": {"default_negotiation_style": "high_context_formal"},
    "ZA": {"default_negotiation_style": "consensus"},
    "NG": {"default_negotiation_style": "market_bargaining"},
}
REGIONS = tuple(
    dataclasses.replace(r, **_OVERRIDES[r.code]) if r.code in _OVERRIDES else r
    for r in REGIONS
)

_BY_CODE = {r.code: r for r in REGIONS}


def all_regions() -> List[Region]:
    return list(REGIONS)


def lookup(code: Optional[str]) -> Optional[Region]:
    if not code:
        return None
    if code.upper() == DEFAULT_REGION.code:
        return DEFAULT_REGION
    return _BY_CODE.get(code.upper())


def region_for_point(lat: float, lon: float) -> Region:
    """Best-effort region for a ``(lat, lon)``; falls back to the Africa default."""
    for region in REGIONS:
        lon_min, lat_min, lon_max, lat_max = region.bbox
        if lon_min <= lon <= lon_max and lat_min <= lat <= lat_max:
            return region
    return DEFAULT_REGION


def region_for_coords(coords: Optional[dict]) -> Optional[Region]:
    """Region for a ``{"lat","lon"}`` dict, or ``None`` when coords are absent."""
    if not coords or coords.get("lat") is None or coords.get("lon") is None:
        return None
    return region_for_point(coords["lat"], coords["lon"])
