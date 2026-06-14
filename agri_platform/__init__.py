"""Agri-Travel GIS Drone Platform.

A modular platform that bridges agriculture (weather, growing-degree-days,
soil monitoring, drone surveys) with travel/logistics (hazard-aware vehicle
routing). It is split into three independently deployable services that share a
small common core library:

* ``agri_platform.wfaas`` -- Weather Forecast & Agronomy as a Service (WFAAS),
  customer brand **prisaForecast**
* ``agri_platform.traas`` -- Travel / Routing as a Service (TRAAS), customer
  brand **prisaTravel**
* ``agri_platform.marketplace`` -- Logistics as a Service (LGaaS), customer
  brand **prisaMove**: loads, vehicles, offers and AI-driven culturally-aware
  haggling

The :mod:`agri_platform.common` package holds dependency-light domain logic
(geospatial helpers, GDD calculation, the Open-Meteo client and database
plumbing) that both services reuse.
"""

__version__ = "0.1.0"
