"""SQLAlchemy models for WFAAS."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String
from sqlalchemy.orm import declarative_base
from sqlalchemy.types import JSON

Base = declarative_base()


class Farm(Base):
    __tablename__ = "farms"

    id = Column(Integer, primary_key=True)
    farm_id = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(100))
    coordinates = Column(JSON)  # {"lat": float, "lon": float}
    owner = Column(String(100))
    owner_email = Column(String(120))
    drone_fleet = Column(JSON, default=list)  # list of drone_id
    monitoring_schedule = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "farm_id": self.farm_id,
            "name": self.name,
            "coordinates": self.coordinates,
            "owner": self.owner,
            "owner_email": self.owner_email,
            "drone_fleet": self.drone_fleet or [],
            "monitoring_schedule": self.monitoring_schedule or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class WeatherAlert(Base):
    __tablename__ = "weather_alerts"

    id = Column(Integer, primary_key=True)
    farm_id = Column(String(50), index=True)
    alert_type = Column(String(50))
    severity = Column(String(20))  # Low | Medium | High | Critical
    message = Column(String(500))
    coordinates = Column(JSON)
    timestamp = Column(DateTime, default=datetime.utcnow)
    resolved = Column(Boolean, default=False)
    related_data = Column(JSON)
    confidence = Column(Float)

    def to_dict(self):
        return {
            "id": self.id,
            "farm_id": self.farm_id,
            "alert_type": self.alert_type,
            "severity": self.severity,
            "message": self.message,
            "coordinates": self.coordinates,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "resolved": self.resolved,
            "related_data": self.related_data,
            "confidence": self.confidence,
        }


class AlertPreference(Base):
    __tablename__ = "alert_preferences"

    id = Column(Integer, primary_key=True)
    farm_id = Column(String(50), unique=True, index=True)
    alert_types = Column(JSON, default=list)
    notification_frequency = Column(String(20), default="immediate")
    email_enabled = Column(Boolean, default=True)
    sms_enabled = Column(Boolean, default=False)

    def to_dict(self):
        return {
            "farm_id": self.farm_id,
            "alert_types": self.alert_types or [],
            "notification_frequency": self.notification_frequency,
            "email_enabled": self.email_enabled,
            "sms_enabled": self.sms_enabled,
        }


class Drone(Base):
    __tablename__ = "drones"

    id = Column(Integer, primary_key=True)
    drone_id = Column(String(50), unique=True, nullable=False, index=True)
    model = Column(String(100))
    battery_level = Column(Float, default=100.0)
    status = Column(String(20), default="available")  # available|in_mission|charging|maintenance
    capabilities = Column(JSON, default=list)
    location = Column(JSON)
    assigned_farm = Column(String(50))
    last_maintenance = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "drone_id": self.drone_id,
            "model": self.model,
            "battery_level": self.battery_level,
            "status": self.status,
            "capabilities": self.capabilities or [],
            "location": self.location,
            "assigned_farm": self.assigned_farm,
            "last_maintenance": self.last_maintenance.isoformat() if self.last_maintenance else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class DroneMission(Base):
    __tablename__ = "drone_missions"

    id = Column(Integer, primary_key=True)
    mission_id = Column(String(50), unique=True, nullable=False, index=True)
    farm_id = Column(String(50), index=True)
    drone_id = Column(String(50), index=True)
    status = Column(String(20), default="pending")  # pending|in_progress|completed|failed
    mission_type = Column(String(50))  # health_scan|crop_monitoring|hazard_detection
    waypoints = Column(JSON)
    payload = Column(JSON)
    results = Column(JSON)
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "mission_id": self.mission_id,
            "farm_id": self.farm_id,
            "drone_id": self.drone_id,
            "status": self.status,
            "mission_type": self.mission_type,
            "waypoints": self.waypoints,
            "payload": self.payload,
            "results": self.results,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
