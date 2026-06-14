#!/usr/bin/env python3
"""Operational CLI for the agri-travel platform.

Examples:
    python manage.py init-db wfaas
    python manage.py init-db traas
    python manage.py seed                 # demo farm, drone, hazard
    python manage.py scan                 # run one WFAAS hazard scan
"""

from __future__ import annotations

import argparse
import sys

from agri_platform.common import db as db_helpers
from agri_platform.common.config import Settings


def _factory(service: str):
    settings = Settings.from_env(service, default_port=0)
    engine, factory = db_helpers.make_session_factory(settings.database_url)
    return settings, engine, factory


def cmd_init_db(args) -> int:
    service = args.service
    if service == "wfaas":
        from agri_platform.wfaas.models import Base
    elif service == "traas":
        from agri_platform.traas.models import Base
    else:
        print(f"unknown service: {service}", file=sys.stderr)
        return 2
    _, engine, _ = _factory(service)
    db_helpers.init_models(engine, Base, drop=args.drop)
    print(f"{service}: schema created ({'recreated' if args.drop else 'ensured'}) at {engine.url}")
    return 0


def cmd_seed(args) -> int:
    from agri_platform.wfaas.models import Base as WBase, Drone, Farm
    from agri_platform.traas.models import Base as TBase, Hazard

    _, weng, wfac = _factory("wfaas")
    db_helpers.init_models(weng, WBase)
    ws = wfac()
    if not ws.query(Farm).filter_by(farm_id="demo-farm").first():
        ws.add(Farm(farm_id="demo-farm", name="Demo Farm", owner="Demo Owner",
                    owner_email="demo@example.com", coordinates={"lat": 10.5105, "lon": 7.4165},
                    drone_fleet=[], monitoring_schedule={}))
    if not ws.query(Drone).filter_by(drone_id="demo-drone").first():
        ws.add(Drone(drone_id="demo-drone", model="DJI Agras", capabilities=["NDVI", "camera", "gps"]))
    ws.commit()

    _, teng, tfac = _factory("traas")
    db_helpers.init_models(teng, TBase)
    ts = tfac()
    if not ts.query(Hazard).filter_by(hazard_id="demo-hazard").first():
        ts.add(Hazard(hazard_id="demo-hazard", hazard_type="safety", severity="Critical",
                      location={"lat": 10.8, "lon": 7.6}, radius_km=3.0, status="active"))
    ts.commit()
    print("seeded demo farm, drone and hazard")
    return 0


def cmd_scan(args) -> int:
    from agri_platform.common.weather import OpenMeteoClient
    from agri_platform.wfaas.models import Base
    from agri_platform.wfaas.monitoring import scan_farms
    from agri_platform.wfaas.notifications import make_notifier

    settings, engine, factory = _factory("wfaas")
    db_helpers.init_models(engine, Base)
    alerts = scan_farms(factory(), OpenMeteoClient(settings.open_meteo_url), make_notifier(settings))
    print(f"scan complete: {len(alerts)} alert(s) created")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agri-travel platform management CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-db", help="create database schema for a service")
    p_init.add_argument("service", choices=["wfaas", "traas"])
    p_init.add_argument("--drop", action="store_true", help="drop existing tables first")
    p_init.set_defaults(func=cmd_init_db)

    sub.add_parser("seed", help="insert demo data").set_defaults(func=cmd_seed)
    sub.add_parser("scan", help="run one WFAAS hazard scan").set_defaults(func=cmd_scan)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
