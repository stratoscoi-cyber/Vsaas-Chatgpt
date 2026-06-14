"""SQLAlchemy models for TRAAS."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String
from sqlalchemy.orm import declarative_base
from sqlalchemy.types import JSON

Base = declarative_base()


class Route(Base):
    __tablename__ = "routes"

    id = Column(Integer, primary_key=True)
    route_id = Column(String(50), unique=True, nullable=False, index=True)
    origin = Column(JSON)  # [lat, lon]
    destination = Column(JSON)  # [lat, lon]
    route_type = Column(String(30))  # heavy_truck|agricultural_vehicle|light_vehicle
    path = Column(JSON)  # list of [lat, lon]
    distance_km = Column(Float)
    duration_min = Column(Float)
    hazards = Column(JSON)
    status = Column(String(20), default="active")
    timestamp = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "route_id": self.route_id,
            "origin": self.origin,
            "destination": self.destination,
            "route_type": self.route_type,
            "path": self.path,
            "distance_km": self.distance_km,
            "duration_min": self.duration_min,
            "hazards": self.hazards,
            "status": self.status,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }


class Hazard(Base):
    __tablename__ = "hazards"

    id = Column(Integer, primary_key=True)
    hazard_id = Column(String(50), unique=True, nullable=False, index=True)
    hazard_type = Column(String(50))  # weather|traffic|safety|red_zone|flood
    severity = Column(String(20))
    location = Column(JSON)  # {"lat": float, "lon": float}
    geometry = Column(JSON)  # optional GeoJSON polygon
    radius_km = Column(Float, default=2.0)
    status = Column(String(20), default="active")
    timestamp = Column(DateTime, default=datetime.utcnow)
    valid_until = Column(DateTime)

    def to_dict(self):
        return {
            "hazard_id": self.hazard_id,
            "hazard_type": self.hazard_type,
            "severity": self.severity,
            "location": self.location,
            "geometry": self.geometry,
            "radius_km": self.radius_km,
            "status": self.status,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
        }


class TransportConstraint(Base):
    __tablename__ = "transport_constraints"

    id = Column(Integer, primary_key=True)
    farm_id = Column(String(50), index=True)
    vehicle_type = Column(String(30))
    constraints = Column(JSON)
    timestamp = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "farm_id": self.farm_id,
            "vehicle_type": self.vehicle_type,
            "constraints": self.constraints,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }
