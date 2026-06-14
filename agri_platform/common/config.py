"""Centralised, environment-driven configuration.

Every tunable is read once from the environment into an immutable
:class:`Settings` object, so the rest of the code never touches ``os.environ``
directly and tests can construct explicit settings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import FrozenSet, Optional


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    val = os.getenv(name)
    try:
        return int(val) if val is not None else default
    except ValueError:
        return default


def _keys(name: str) -> FrozenSet[str]:
    raw = os.getenv(name, "")
    return frozenset(k.strip() for k in raw.split(",") if k.strip())


@dataclass(frozen=True)
class Settings:
    service: str
    database_url: str
    # Security
    api_keys: FrozenSet[str] = field(default_factory=frozenset)
    admin_api_keys: FrozenSet[str] = field(default_factory=frozenset)
    auth_enabled: bool = False
    # Rate limiting (requests / minute / client). 0 disables.
    rate_limit_per_minute: int = 0
    # Caching
    redis_url: Optional[str] = None
    cache_ttl_seconds: int = 300
    # Upstreams
    open_meteo_url: str = "https://api.open-meteo.com/v1/forecast"
    valhalla_url: Optional[str] = None
    # TRAAS routing service (used by LGaaS reroute to compute avoidance paths)
    traas_url: Optional[str] = None
    # Localization (Africa-centric defaults)
    default_language: str = "en"
    default_region: str = "NG"
    # Public-holiday source: "admin" (table only) or "library" (maintained
    # python-holidays calendars layered under the admin table).
    holiday_provider: str = "admin"
    # Observability
    log_level: str = "INFO"
    log_json: bool = True
    # Notifications (SMTP)
    notifications_enabled: bool = False
    smtp_host: str = "localhost"
    smtp_port: int = 25
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from: str = "alerts@agri-platform.local"
    smtp_use_tls: bool = False
    # Background monitoring scheduler
    monitor_enabled: bool = False
    monitor_interval_minutes: int = 30
    # Drone imagery inference endpoint (external model service)
    drone_ai_endpoint: Optional[str] = None
    # LGaaS / prisaMove haggling message composer endpoint (optional LLM service)
    haggle_ai_endpoint: Optional[str] = None
    # Optional webhook for shipment tracking notifications (delay/reroute/ETA)
    tracking_webhook_url: Optional[str] = None
    # Enforce vehicle/driver onboarding compliance before allowing offers
    compliance_enforced: bool = False
    # Outbound notification channel provider webhooks (SMS / WhatsApp / push)
    sms_webhook_url: Optional[str] = None
    whatsapp_webhook_url: Optional[str] = None
    push_webhook_url: Optional[str] = None
    # KYC / document-verification & sanctions-screening provider endpoints
    kyc_endpoint: Optional[str] = None
    sanctions_endpoint: Optional[str] = None
    # Row-level multi-tenant isolation (scopes data by X-Tenant-ID)
    tenant_isolation: bool = False
    # HTTP
    port: int = 5000

    @classmethod
    def from_env(cls, service: str, default_port: int) -> "Settings":
        default_db = f"sqlite:///{service}.db"
        return cls(
            service=service,
            database_url=os.getenv("DATABASE_URL", default_db),
            api_keys=_keys("API_KEYS"),
            admin_api_keys=_keys("ADMIN_API_KEYS"),
            auth_enabled=_bool("AUTH_ENABLED", False),
            rate_limit_per_minute=_int("RATE_LIMIT_PER_MINUTE", 0),
            redis_url=os.getenv("REDIS_URL"),
            cache_ttl_seconds=_int("CACHE_TTL_SECONDS", 300),
            open_meteo_url=os.getenv("OPEN_METEO_URL", "https://api.open-meteo.com/v1/forecast"),
            valhalla_url=os.getenv("VALHALLA_URL"),
            traas_url=os.getenv("TRAAS_URL"),
            default_language=os.getenv("DEFAULT_LANGUAGE", "en"),
            default_region=os.getenv("DEFAULT_REGION", "NG"),
            holiday_provider=os.getenv("HOLIDAY_PROVIDER", "admin"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            log_json=_bool("LOG_JSON", True),
            notifications_enabled=_bool("NOTIFICATIONS_ENABLED", False),
            smtp_host=os.getenv("SMTP_HOST", "localhost"),
            smtp_port=_int("SMTP_PORT", 25),
            smtp_user=os.getenv("SMTP_USER"),
            smtp_password=os.getenv("SMTP_PASSWORD"),
            smtp_from=os.getenv("SMTP_FROM", "alerts@agri-platform.local"),
            smtp_use_tls=_bool("SMTP_USE_TLS", False),
            monitor_enabled=_bool("MONITOR_ENABLED", False),
            monitor_interval_minutes=_int("MONITOR_INTERVAL_MINUTES", 30),
            drone_ai_endpoint=os.getenv("DRONE_AI_ENDPOINT"),
            haggle_ai_endpoint=os.getenv("HAGGLE_AI_ENDPOINT"),
            tracking_webhook_url=os.getenv("TRACKING_WEBHOOK_URL"),
            compliance_enforced=_bool("COMPLIANCE_ENFORCED", False),
            sms_webhook_url=os.getenv("SMS_WEBHOOK_URL"),
            whatsapp_webhook_url=os.getenv("WHATSAPP_WEBHOOK_URL"),
            push_webhook_url=os.getenv("PUSH_WEBHOOK_URL"),
            kyc_endpoint=os.getenv("KYC_ENDPOINT"),
            sanctions_endpoint=os.getenv("SANCTIONS_ENDPOINT"),
            tenant_isolation=_bool("TENANT_ISOLATION", False),
            port=_int("PORT", default_port),
        )

    def with_overrides(self, **kwargs) -> "Settings":
        return replace(self, **kwargs)
