"""
FinGeoRisk — Actuarial Geospatial Asset Terminal (backend)
==========================================================
Actuarial financial analytics + interactive global locations, now with:
  - an auto "why this area is at risk" summary
  - colour-coded risk hotspots
  - real-time disaster monitoring (USGS earthquakes + NASA EONET)
"""

import os
import time
import json
import importlib
import requests
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, send_from_directory
try:
    load_dotenv = importlib.import_module("dotenv").load_dotenv
except Exception:
    def load_dotenv(*_args, **_kwargs):
        return False

app = Flask(__name__, static_folder=None)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
load_dotenv(os.path.join(BASE_DIR, ".env"))

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
ENV_API_KEY = os.environ.get("GEMINI_API_KEY", "")
RENTCAST_API_KEY = os.environ.get("RENTCAST_API_KEY", "")
NASA_FIRMS_MAP_KEY = os.environ.get("NASA_FIRMS_MAP_KEY", "")
USER_AGENT = "FinGeoRisk/3.0 (actuarial demo)"
AVM_CACHE_TTL_SEC = int(os.environ.get("AVM_CACHE_TTL_SEC", "1800"))
_AVM_CACHE = {}


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def clip(value, lo, hi):
    return max(lo, min(hi, value))


def baseline_property_profile(address):
    q = (address or "").lower()
    if "san francisco" in q or " ca" in q:
        return {
            "propertyValue": 1850000,
            "yearBuilt": 1978,
            "squareFootage": 2100,
            "constructionType": "Steel Frame",
            "lastSalePrice": 1525000,
        }
    if "miami" in q or " fl" in q:
        return {
            "propertyValue": 1125000,
            "yearBuilt": 1998,
            "squareFootage": 2450,
            "constructionType": "Concrete Block",
            "lastSalePrice": 935000,
        }
    if "dallas" in q or " tx" in q:
        return {
            "propertyValue": 520000,
            "yearBuilt": 2007,
            "squareFootage": 2600,
            "constructionType": "Brick Veneer",
            "lastSalePrice": 435000,
        }
    return {
        "propertyValue": 445000,
        "yearBuilt": 2003,
        "squareFootage": 1950,
        "constructionType": "Wood Frame",
        "lastSalePrice": 372000,
    }


def get_cached_avm(address):
    key = (address or "").strip().lower()
    if not key:
        return None
    cached = _AVM_CACHE.get(key)
    if not cached:
        return None
    if (time.time() - cached["t"]) > AVM_CACHE_TTL_SEC:
        _AVM_CACHE.pop(key, None)
        return None
    return cached


def fetch_live_avm(address):
    endpoint = "https://api.rentcast.io/v1/avm/value"
    try:
        resp = requests.get(
            endpoint,
            params={"address": address},
            headers={
                "X-Api-Key": RENTCAST_API_KEY,
                "accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            timeout=20,
        )
        if resp.status_code != 200:
            return {
                "ok": False,
                "status": resp.status_code,
                "error": "AVM provider rejected request",
            }

        payload = resp.json() if resp.content else {}
        price = payload.get("price") if isinstance(payload, dict) else None
        if not price:
            return {"ok": False, "status": 204, "error": "AVM provider returned no valuation"}

        key = address.strip().lower()
        _AVM_CACHE[key] = {
            "t": time.time(),
            "price": float(price),
            "raw": payload,
        }
        return {"ok": True, "price": float(price), "raw": payload}
    except Exception:
        return {"ok": False, "status": 502, "error": "AVM request failed"}


def resolve_property_profile(address):
    profile = baseline_property_profile(address)
    source = "baseline"
    provider_status = "no-live-valuation"

    cached = get_cached_avm(address)
    if cached:
        profile["propertyValue"] = float(cached["price"])
        source = "cache"
        provider_status = "cached-valuation"
        return profile, source, provider_status

    live = fetch_live_avm(address)
    if live.get("ok"):
        profile["propertyValue"] = float(live["price"])
        source = "live"
        provider_status = "live-valuation"
    else:
        provider_status = f"provider-unavailable-{live.get('status', 502)}"

    return profile, source, provider_status


# ---------------------------------------------------------------------------
# Real hazard / market data overlays (free public sources, cached)
# ---------------------------------------------------------------------------
US_STATE_NAME_TO_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC",
}

REVERSE_GEO_CACHE_TTL_SEC = int(os.environ.get("REVERSE_GEO_CACHE_TTL_SEC", "604800"))
_REVERSE_GEO_CACHE = {}


def reverse_geocode_state(lat, lon):
    """lat/lon -> {country_code, state_name, state_abbr} via Nominatim. Free, no key."""
    key = f"{lat:.2f},{lon:.2f}"
    cached = _REVERSE_GEO_CACHE.get(key)
    if cached and (time.time() - cached["t"]) < REVERSE_GEO_CACHE_TTL_SEC:
        return cached["data"]
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"format": "json", "lat": lat, "lon": lon, "zoom": 5, "addressdetails": 1},
            headers={"User-Agent": USER_AGENT}, timeout=12,
        )
        resp.raise_for_status()
        addr = resp.json().get("address", {})
        country_code = (addr.get("country_code") or "").upper()
        state_name = addr.get("state")
        iso = addr.get("ISO3166-2-lvl4", "")
        state_abbr = iso.split("-")[-1] if "-" in iso else US_STATE_NAME_TO_ABBR.get((state_name or "").lower())
        data = {"ok": True, "country_code": country_code, "state_name": state_name, "state_abbr": state_abbr}
    except Exception:
        data = {"ok": False, "country_code": None, "state_name": None, "state_abbr": None}
    _REVERSE_GEO_CACHE[key] = {"t": time.time(), "data": data}
    return data


NFHL_CACHE_TTL_SEC = int(os.environ.get("NFHL_CACHE_TTL_SEC", "86400"))
_NFHL_CACHE = {}
FEMA_FLOOD_ZONE_SCORE = {
    "VE": 95, "V": 90, "AE": 75, "A": 70, "AO": 65, "AH": 60,
    "AR": 55, "A99": 50, "X": 15, "D": 35,
}


def fetch_fema_flood_zone(lat, lon):
    """Point-query the FEMA National Flood Hazard Layer for a real flood zone. Free, no key."""
    key = f"{lat:.4f},{lon:.4f}"
    cached = _NFHL_CACHE.get(key)
    if cached and (time.time() - cached["t"]) < NFHL_CACHE_TTL_SEC:
        return cached["data"]
    pad = 0.001
    try:
        resp = requests.get(
            "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/identify",
            params={
                "geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "sr": 4326,
                "layers": "all:28", "tolerance": 2,
                "mapExtent": f"{lon-pad},{lat-pad},{lon+pad},{lat+pad}",
                "imageDisplay": "400,400,96", "returnGeometry": "false", "f": "json",
            },
            headers={"User-Agent": USER_AGENT}, timeout=12,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            data = {"ok": True, "in_coverage": False, "zone": None, "source": "FEMA NFHL (US)"}
        else:
            attrs = results[0].get("attributes", {})
            zone = attrs.get("FLD_ZONE")
            data = {
                "ok": True, "in_coverage": True, "zone": zone,
                "sfha": attrs.get("SFHA_TF") == "T", "source": "FEMA NFHL (US)",
            }
    except Exception:
        data = {"ok": False, "in_coverage": None, "zone": None, "source": "FEMA NFHL (US)"}
    _NFHL_CACHE[key] = {"t": time.time(), "data": data}
    return data


WHP_CACHE_TTL_SEC = int(os.environ.get("WHP_CACHE_TTL_SEC", "86400"))
_WHP_CACHE = {}
WHP_CLASS_LABELS = {1: "Very Low", 2: "Low", 3: "Moderate", 4: "High", 5: "Very High",
                     6: "Non-burnable", 7: "Water"}
WHP_CLASS_SCORE = {1: 8, 2: 22, 3: 45, 4: 68, 5: 88, 6: 3, 7: 1}


def fetch_usfs_whp(lat, lon):
    """Point-query USFS Wildfire Hazard Potential for a real wildfire hazard class. Free, no key."""
    key = f"{lat:.4f},{lon:.4f}"
    cached = _WHP_CACHE.get(key)
    if cached and (time.time() - cached["t"]) < WHP_CACHE_TTL_SEC:
        return cached["data"]
    try:
        geometry = json.dumps({"x": lon, "y": lat, "spatialReference": {"wkid": 4326}})
        resp = requests.get(
            "https://imagery.geoplatform.gov/iipp/rest/services/Fire_Aviation/"
            "USFS_EDW_RMRS_WildfireHazardPotentialClassified/ImageServer/identify",
            params={"geometry": geometry, "geometryType": "esriGeometryPoint",
                    "returnGeometry": "false", "f": "json"},
            headers={"User-Agent": USER_AGENT}, timeout=12,
        )
        resp.raise_for_status()
        raw_value = resp.json().get("value")
        whp_class = int(raw_value) if raw_value not in (None, "NoData", "255") else None
        if whp_class is None or whp_class not in WHP_CLASS_LABELS:
            data = {"ok": True, "in_coverage": False, "class": None, "source": "USFS WHP (US)"}
        else:
            data = {
                "ok": True, "in_coverage": True, "class": whp_class,
                "label": WHP_CLASS_LABELS[whp_class], "source": "USFS WHP (US)",
            }
    except Exception:
        data = {"ok": False, "in_coverage": None, "class": None, "source": "USFS WHP (US)"}
    _WHP_CACHE[key] = {"t": time.time(), "data": data}
    return data


WIND_GUST_CACHE_TTL_SEC = int(os.environ.get("WIND_GUST_CACHE_TTL_SEC", "86400"))
_WIND_GUST_CACHE = {}


def fetch_historical_wind_gust(lat, lon):
    """10yr max recorded wind gust at a point via Open-Meteo's historical archive. Free, no key, global."""
    key = f"{lat:.2f},{lon:.2f}"
    cached = _WIND_GUST_CACHE.get(key)
    if cached and (time.time() - cached["t"]) < WIND_GUST_CACHE_TTL_SEC:
        return cached["data"]
    try:
        end_date = datetime.now(timezone.utc).date() - timedelta(days=2)
        start_date = end_date - timedelta(days=365 * 10)
        resp = requests.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={
                "latitude": lat, "longitude": lon,
                "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
                "daily": "wind_gusts_10m_max", "timezone": "UTC",
            },
            headers={"User-Agent": USER_AGENT}, timeout=20,
        )
        resp.raise_for_status()
        gusts = [g for g in resp.json().get("daily", {}).get("wind_gusts_10m_max", []) if g is not None]
        if not gusts:
            data = {"ok": False, "max_gust_kmh": None, "source": "Open-Meteo historical archive"}
        else:
            data = {"ok": True, "max_gust_kmh": round(max(gusts), 1), "source": "Open-Meteo historical archive"}
    except Exception:
        data = {"ok": False, "max_gust_kmh": None, "source": "Open-Meteo historical archive"}
    _WIND_GUST_CACHE[key] = {"t": time.time(), "data": data}
    return data


FHFA_HPI_CACHE_TTL_SEC = int(os.environ.get("FHFA_HPI_CACHE_TTL_SEC", "604800"))
_FHFA_HPI_TABLE_CACHE = {"t": 0, "by_state": {}}


def _load_fhfa_hpi_table():
    """Download & parse FHFA's free, no-key state House Price Index CSV (quarterly, since 1975)."""
    now = time.time()
    if _FHFA_HPI_TABLE_CACHE["by_state"] and (now - _FHFA_HPI_TABLE_CACHE["t"]) < FHFA_HPI_CACHE_TTL_SEC:
        return _FHFA_HPI_TABLE_CACHE["by_state"]
    try:
        resp = requests.get(
            "https://www.fhfa.gov/hpi/download/quarterly_datasets/hpi_at_state.csv",
            headers={"User-Agent": USER_AGENT}, timeout=20,
        )
        resp.raise_for_status()
        by_state = {}
        for line in resp.text.splitlines():
            parts = line.strip().split(",")
            if len(parts) != 4:
                continue
            state, year, quarter, value = parts
            try:
                row = (int(year), int(quarter), float(value))
            except ValueError:
                continue
            by_state.setdefault(state, []).append(row)
        for series in by_state.values():
            series.sort()
        if by_state:
            _FHFA_HPI_TABLE_CACHE.update({"t": now, "by_state": by_state})
        return by_state
    except Exception:
        return _FHFA_HPI_TABLE_CACHE["by_state"]


def fetch_fhfa_state_hpi(state_abbr):
    """Real YoY %% change in a US state's FHFA House Price Index. Free, no key, no signup."""
    if not state_abbr:
        return {"ok": False, "yoy_pct": None, "source": "FHFA House Price Index (US)"}
    table = _load_fhfa_hpi_table()
    series = table.get(state_abbr.upper())
    if not series or len(series) < 5:
        return {"ok": False, "yoy_pct": None, "source": "FHFA House Price Index (US)"}
    latest_year, latest_q, latest_val = series[-1]
    prior = next((row for row in reversed(series[:-1])
                  if row[0] == latest_year - 1 and row[1] == latest_q), None)
    if not prior or not prior[2]:
        return {"ok": False, "yoy_pct": None, "source": "FHFA House Price Index (US)"}
    yoy_pct = (latest_val - prior[2]) / prior[2] * 100
    return {"ok": True, "yoy_pct": round(yoy_pct, 2),
            "as_of": f"{latest_year}Q{latest_q}", "source": "FHFA House Price Index (US)"}


FIRMS_CACHE_TTL_SEC = int(os.environ.get("FIRMS_CACHE_TTL_SEC", "1800"))
_FIRMS_CACHE = {}


def fetch_nasa_firms_activity(lat, lon, radius_deg=0.5, day_range=3):
    """Real-time active fire detections near a point via NASA FIRMS. Supplementary signal only —
    reflects fires burning right now, NOT a long-term wildfire hazard score. Needs a free MAP_KEY."""
    if not NASA_FIRMS_MAP_KEY:
        return {"ok": False, "active": False, "count": 0, "source": "NASA FIRMS"}
    key = f"{lat:.2f},{lon:.2f}"
    cached = _FIRMS_CACHE.get(key)
    if cached and (time.time() - cached["t"]) < FIRMS_CACHE_TTL_SEC:
        return cached["data"]
    try:
        bbox = f"{lon-radius_deg},{lat-radius_deg},{lon+radius_deg},{lat+radius_deg}"
        resp = requests.get(
            f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{NASA_FIRMS_MAP_KEY}/VIIRS_SNPP_NRT/{bbox}/{day_range}",
            headers={"User-Agent": USER_AGENT}, timeout=20,
        )
        resp.raise_for_status()
        rows = [line for line in resp.text.splitlines() if line and not line.startswith("latitude")]
        if resp.text.strip().lower().startswith(("invalid", "error")):
            data = {"ok": False, "active": False, "count": 0, "source": "NASA FIRMS"}
        else:
            data = {"ok": True, "active": len(rows) > 0, "count": len(rows), "source": "NASA FIRMS"}
    except Exception:
        data = {"ok": False, "active": False, "count": 0, "source": "NASA FIRMS"}
    _FIRMS_CACHE[key] = {"t": time.time(), "data": data}
    return data


# ---------------------------------------------------------------------------
# Disaster scenario simulation (real elevation + real current wind, then
# published reference formulas/tables for storm surge, fire spread, wind
# radii, EF-scale statistics, and MMI attenuation — see backend/Readme.md)
# ---------------------------------------------------------------------------
ELEVATION_CACHE_TTL_SEC = int(os.environ.get("ELEVATION_CACHE_TTL_SEC", "604800"))
_ELEVATION_CACHE = {}

# Typical storm-surge height ranges by Saffir-Simpson category (NOAA reference).
SURGE_HEIGHT_M = {1: 1.2, 2: 1.8, 3: 2.7, 4: 4.0, 5: 5.5}

# Simplified FEMA/USACE-style depth-damage curve for residential structures.
FLOOD_DEPTH_DAMAGE_FT = [(0, 0.0), (1, 0.10), (2, 0.20), (3, 0.32), (4, 0.40), (6, 0.50), (8, 0.60)]


def fetch_elevation_grid(points):
    """Real elevation (meters) for up to 100 lat/lon points via Open-Meteo. Free, no key."""
    if not points:
        return []
    key = tuple((round(p[0], 4), round(p[1], 4)) for p in points)
    cached = _ELEVATION_CACHE.get(key)
    if cached and (time.time() - cached["t"]) < ELEVATION_CACHE_TTL_SEC:
        return cached["data"]
    try:
        lats = ",".join(str(p[0]) for p in points)
        lons = ",".join(str(p[1]) for p in points)
        resp = requests.get(
            "https://api.open-meteo.com/v1/elevation",
            params={"latitude": lats, "longitude": lons},
            headers={"User-Agent": USER_AGENT}, timeout=20,
        )
        resp.raise_for_status()
        elevations = resp.json().get("elevation", [])
    except Exception:
        elevations = []
    if len(elevations) != len(points):
        elevations = [None] * len(points)
    _ELEVATION_CACHE[key] = {"t": time.time(), "data": elevations}
    return elevations


def flood_damage_ratio(depth_m):
    """Simplified FEMA/USACE-style residential depth-damage ratio for a given flood depth."""
    if depth_m <= 0:
        return 0.0
    depth_ft = depth_m * 3.28084
    for (ft_lo, ratio_lo), (ft_hi, ratio_hi) in zip(FLOOD_DEPTH_DAMAGE_FT, FLOOD_DEPTH_DAMAGE_FT[1:]):
        if depth_ft <= ft_hi:
            span = ft_hi - ft_lo
            frac = (depth_ft - ft_lo) / span if span else 0
            return ratio_lo + frac * (ratio_hi - ratio_lo)
    return FLOOD_DEPTH_DAMAGE_FT[-1][1]


MAX_SURGE_REACH_M = 30.0  # storm surge has never realistically exceeded this; above it, surge simply doesn't apply


def simulate_flood(lat, lon, category):
    """Real-elevation grid bathtub model around a point for a given hurricane category's storm surge.

    Storm surge floods land at or below a given ABSOLUTE elevation above sea level (NOAA's typical
    surge-height reference for that category) — it is not relative to the clicked point's own elevation,
    so inland/high-elevation origins correctly show no flooding.
    """
    category = clip(int(category), 1, 5)
    surge_m = SURGE_HEIGHT_M[category]
    grid_n = 10
    span_deg = 0.02 + (category * 0.01)  # bigger storms flood a wider illustrative radius
    lat_step = (span_deg * 2) / (grid_n - 1)
    lon_step = lat_step
    points = [(lat, lon)]
    for i in range(grid_n):
        for j in range(grid_n):
            if len(points) >= 100:
                break
            points.append((lat - span_deg + i * lat_step, lon - span_deg + j * lon_step))

    elevations = fetch_elevation_grid(points)
    origin_elev = elevations[0] if elevations else None
    grid_points, grid_elevations = points[1:], elevations[1:]

    if origin_elev is None or origin_elev > MAX_SURGE_REACH_M:
        return {
            "ok": True,
            "hazard": "flood",
            "category": category,
            "surge_height_m": surge_m,
            "surge_applicable": False,
            "origin_elevation_m": origin_elev,
            "cells": [],
            "area_km2": 0.0,
            "peak_depth_m": 0.0,
            "damage_ratio_at_origin": 0.0,
            "source": "Open-Meteo Elevation API (real) + NOAA typical surge-height reference",
        }

    cells = []
    flooded_count = 0
    for (plat, plon), elev in zip(grid_points, grid_elevations):
        if elev is None:
            continue
        depth = surge_m - elev
        flooded = depth > 0
        if flooded:
            flooded_count += 1
        cells.append({"lat": plat, "lon": plon, "elevation_m": round(elev, 1),
                       "depth_m": round(max(depth, 0), 2), "flooded": flooded})

    cell_area_km2 = ((lat_step * 111.0) * (lon_step * 111.0))
    area_km2 = flooded_count * cell_area_km2
    peak_depth = max((c["depth_m"] for c in cells), default=0.0)
    return {
        "ok": True,
        "hazard": "flood",
        "category": category,
        "surge_height_m": surge_m,
        "surge_applicable": True,
        "origin_elevation_m": round(origin_elev, 1),
        "cells": cells,
        "area_km2": round(area_km2, 3),
        "peak_depth_m": round(peak_depth, 2),
        "damage_ratio_at_origin": round(flood_damage_ratio(max(surge_m - origin_elev, 0)), 3),
        "source": "Open-Meteo Elevation API (real) + NOAA typical surge-height reference",
    }


def fetch_current_wind(lat, lon):
    """Real current wind speed (km/h) and direction (deg) at a point via Open-Meteo. Free, no key."""
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": lat, "longitude": lon,
                    "current": "wind_speed_10m,wind_direction_10m"},
            headers={"User-Agent": USER_AGENT}, timeout=15,
        )
        resp.raise_for_status()
        current = resp.json().get("current", {})
        speed = current.get("wind_speed_10m")
        direction = current.get("wind_direction_10m")
        if speed is None or direction is None:
            return {"ok": False, "speed_kmh": None, "direction_deg": None, "source": "Open-Meteo forecast"}
        return {"ok": True, "speed_kmh": float(speed), "direction_deg": float(direction),
                "source": "Open-Meteo forecast"}
    except Exception:
        return {"ok": False, "speed_kmh": None, "direction_deg": None, "source": "Open-Meteo forecast"}


# ---------------------------------------------------------------------------
# Actuarial model
# ---------------------------------------------------------------------------
def compute_actuarial_metrics(lat, lon, location_name="Selected Property", live_overlays=True):
    """Deterministic hyper-local cat-model -> financial underwriting parameters.

    When live_overlays is True, real free hazard data (FEMA flood zones, USFS
    wildfire hazard potential, historical wind gust extremes) replaces the
    hash-based estimate per-vector wherever it's available; otherwise (and for
    any vector lacking coverage) the model falls back to the hash baseline.
    """
    seed = abs(hash(f"{lat:.3f},{lon:.3f}"))

    coastal = any(w in location_name.lower() for w in ("water", "coast", "beach", "bay", "harbor", "port"))
    base_flood = (seed % 75) + 10 if coastal else (seed % 55) + 5
    base_wind = ((seed >> 2) % 65) + 15
    base_wildfire = ((seed >> 4) % 80) + 5 if base_flood < 30 else (seed % 25)

    flood_prob = clip(base_flood, 0, 100)
    wind_prob = clip(base_wind, 0, 100)
    wildfire_prob = clip(base_wildfire, 0, 100)
    vector_sources = {
        "flood": {"source": "model", "status": "hash-estimated"},
        "wind": {"source": "model", "status": "hash-estimated"},
        "wildfire": {"source": "model", "status": "hash-estimated"},
    }

    fire_activity = {"active": False, "count": 0, "source": "NASA FIRMS", "ok": False}

    if live_overlays:
        with ThreadPoolExecutor(max_workers=4) as pool:
            nfhl_future = pool.submit(fetch_fema_flood_zone, lat, lon)
            whp_future = pool.submit(fetch_usfs_whp, lat, lon)
            wind_future = pool.submit(fetch_historical_wind_gust, lat, lon)
            fire_future = pool.submit(fetch_nasa_firms_activity, lat, lon)
            nfhl, whp, wind_data, fire_activity = (
                nfhl_future.result(), whp_future.result(), wind_future.result(), fire_future.result(),
            )

        if nfhl.get("ok") and nfhl.get("in_coverage"):
            flood_prob = FEMA_FLOOD_ZONE_SCORE.get(nfhl["zone"], flood_prob)
            vector_sources["flood"] = {"source": nfhl["source"], "status": f"live-zone-{nfhl['zone']}"}
        elif nfhl.get("ok") and nfhl.get("in_coverage") is False:
            vector_sources["flood"] = {"source": "model", "status": "hash-estimated-outside-us-coverage"}
        else:
            vector_sources["flood"] = {"source": "model", "status": "hash-estimated-source-unavailable"}

        if whp.get("ok") and whp.get("in_coverage"):
            wildfire_prob = WHP_CLASS_SCORE.get(whp["class"], wildfire_prob)
            vector_sources["wildfire"] = {"source": whp["source"], "status": f"live-class-{whp['label']}"}
        elif whp.get("ok") and whp.get("in_coverage") is False:
            vector_sources["wildfire"] = {"source": "model", "status": "hash-estimated-outside-us-coverage"}
        else:
            vector_sources["wildfire"] = {"source": "model", "status": "hash-estimated-source-unavailable"}

        if wind_data.get("ok"):
            gust = wind_data["max_gust_kmh"]
            wind_prob = clip((gust - 40) * (100 / 160), 5, 98)
            vector_sources["wind"] = {"source": "Open-Meteo historical archive",
                                       "status": f"live-max-gust-{gust:.0f}kmh"}
        else:
            vector_sources["wind"] = {"source": "model", "status": "hash-estimated-source-unavailable"}

    total_insured_value = 1250000 + ((seed % 500) * 5000)
    composite_risk_idx = (flood_prob * 0.45) + (wind_prob * 0.35) + (wildfire_prob * 0.20)
    annual_premium = (total_insured_value * 0.002) * (1 + (composite_risk_idx / 25.0))
    eml_pct = clip(composite_risk_idx * 1.1, 10.0, 95.0)
    estimated_max_loss = total_insured_value * (eml_pct / 100.0)
    reward_pool_index = clip(10.0 - (composite_risk_idx / 10.0), 1.2, 9.8)

    if composite_risk_idx >= 70:
        tier = "CRITICAL EXPOSURE"
    elif composite_risk_idx >= 45:
        tier = "HIGH EXPOSURE"
    elif composite_risk_idx >= 25:
        tier = "MODERATE"
    else:
        tier = "MINIMAL"

    # ----- auto "why is this area at risk" summary -----
    perils = {"flood": flood_prob, "hurricane/wind": wind_prob, "wildfire": wildfire_prob}
    dominant = max(perils, key=perils.get)
    tier_word = tier.replace(" EXPOSURE", "").title()
    summary = (
        f"{location_name.split(',')[0]} sits in a {tier_word} risk tier, "
        f"driven primarily by {dominant} ({perils[dominant]:.0f}% modelled payout probability). "
        f"On a ${total_insured_value:,.0f} asset, a worst-case event could destroy "
        f"{eml_pct:.0f}% of value (~${estimated_max_loss:,.0f}), so the engine targets a "
        f"${annual_premium:,.0f} annual premium. Underwriting yield rating: "
        f"{reward_pool_index:.1f}/10."
    )

    return {
        "geography": {"lat": lat, "lon": lon, "name": location_name},
        "financials": {
            "total_insured_value": round(total_insured_value, 2),
            "annual_premium": round(annual_premium, 2),
            "estimated_max_loss": round(estimated_max_loss, 2),
            "eml_pct": round(eml_pct, 1),
            "underwriting_yield": round(reward_pool_index, 1),
        },
        "vectors": {
            "flood_payout_prob": round(flood_prob, 1),
            "wind_payout_prob": round(wind_prob, 1),
            "wildfire_payout_prob": round(wildfire_prob, 1),
            "composite_idx": round(composite_risk_idx, 1),
        },
        "vector_sources": vector_sources,
        "fire_activity": {
            "active": fire_activity.get("active", False),
            "count": fire_activity.get("count", 0),
            "source": fire_activity.get("source", "NASA FIRMS"),
        },
        "tier": tier,
        "risk_summary": summary,
    }


def estimate_property_finance(lat, lon, location_name="Selected Property", address=""):
    """Create a simple predictive property-finance outlook for a location."""
    metrics = compute_actuarial_metrics(lat, lon, location_name)
    fin = metrics["financials"]
    vec = metrics["vectors"]
    seed = abs(hash(f"forecast:{lat:.3f},{lon:.3f}"))
    target_address = (address or location_name or "").strip()
    profile, source, provider_status = resolve_property_profile(target_address)

    coastal = any(word in location_name.lower() for word in ("water", "coast", "beach", "bay", "harbor", "port", "ocean"))
    growth_bias = 3.8 + (seed % 20) / 10.0
    if coastal:
        growth_bias += 0.7
    if "california" in location_name.lower() or "new york" in location_name.lower() or "texas" in location_name.lower():
        growth_bias += 0.5
    growth_bias = clip(growth_bias - (vec["composite_idx"] / 45.0), 1.8, 9.5)

    appreciation_source, appreciation_status = "model", "hash-estimated-source-unavailable"
    geo = reverse_geocode_state(lat, lon)
    if geo.get("ok") and geo.get("country_code") == "US" and geo.get("state_abbr"):
        hpi = fetch_fhfa_state_hpi(geo["state_abbr"])
        if hpi.get("ok"):
            growth_bias = clip(hpi["yoy_pct"], -5.0, 25.0)
            appreciation_source = hpi["source"]
            appreciation_status = f"live-as-of-{hpi['as_of']}"
    elif geo.get("ok"):
        appreciation_status = "hash-estimated-outside-us-coverage"

    base_value = max(float(profile.get("propertyValue") or 0.0), 1.0)
    monthly_rent = round(base_value * (0.006 + ((seed % 10) / 1000.0)), 2)
    rental_yield = round((monthly_rent * 12 / base_value) * 100, 2)

    if growth_bias >= 7.0:
        outlook = "Accelerating"
    elif growth_bias >= 5.0:
        outlook = "Rising steadily"
    elif growth_bias >= 3.2:
        outlook = "Stable to moderate"
    else:
        outlook = "Cooling"

    demand_level = "High" if rental_yield >= 3.5 else "Moderate" if rental_yield >= 2.2 else "Selective"
    trend_label = "increasing" if growth_bias >= 4.5 else "holding steady" if growth_bias >= 3.2 else "softening"

    summary = (
        f"{location_name.split(',')[0]} shows a {outlook.lower()} market outlook with an estimated {growth_bias:.1f}% annual appreciation trend, "
        f"{trend_label} property values over the next five years. Estimated monthly rent is about ${monthly_rent:,.0f}, "
        f"with a rental yield of {rental_yield:.2f}% and {demand_level.lower()} demand signals."
    )

    return {
        "property_value_estimate": round(base_value, 2),
        "projected_5yr_value": round(base_value * (1 + (growth_bias / 100.0)) ** 5, 2),
        "appreciation_rate_pct": round(growth_bias, 2),
        "appreciation_source": appreciation_source,
        "appreciation_status": appreciation_status,
        "monthly_rent_estimate": round(monthly_rent, 2),
        "rental_yield_pct": round(rental_yield, 2),
        "market_outlook": outlook,
        "demand_level": demand_level,
        "summary": summary,
        "value_source": source,
        "provider_status": provider_status,
    }


# ---------------------------------------------------------------------------
# Risk hotspots (colour-coded on the map)
# ---------------------------------------------------------------------------
HOTSPOT_CITIES = [
    ("Miami, FL", 25.7617, -80.1918), ("New Orleans, LA", 29.9511, -90.0715),
    ("Houston, TX", 29.7604, -95.3698), ("Los Angeles, CA", 34.0522, -118.2437),
    ("San Francisco, CA", 37.7749, -122.4194), ("New York, NY", 40.7128, -74.0060),
    ("Tokyo, Japan", 35.6762, 139.6503), ("Jakarta, Indonesia", -6.2088, 106.8456),
    ("Manila, Philippines", 14.5995, 120.9842), ("Mumbai, India", 19.0760, 72.8777),
    ("Venice, Italy", 45.4408, 12.3155), ("Sydney, Australia", -33.8688, 151.2093),
]


def tier_color(tier):
    return {"CRITICAL EXPOSURE": "#ef4444", "HIGH EXPOSURE": "#fb923c",
            "MODERATE": "#fbbf24", "MINIMAL": "#34d399"}.get(tier, "#34d399")


# ---------------------------------------------------------------------------
# Live disaster feeds (cached)
# ---------------------------------------------------------------------------
_DISASTER_CACHE = {"t": 0, "data": None}
EONET_CAT = {
    "wildfires": "wildfire", "severeStorms": "storm", "floods": "flood",
    "volcanoes": "volcano", "seaLakeIce": "ice", "drought": "drought",
}


def fetch_usgs():
    out = []
    try:
        r = requests.get(
            "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson",
            headers={"User-Agent": USER_AGENT}, timeout=12)
        for f in r.json().get("features", [])[:40]:
            c = f["geometry"]["coordinates"]
            p = f["properties"]
            out.append({"type": "earthquake", "title": p.get("place", "Earthquake"),
                        "lat": c[1], "lon": c[0],
                        "detail": f"M{p.get('mag','?')}", "mag": p.get("mag"),
                        "time": p.get("time"), "source": "USGS"})
    except Exception:
        pass
    return out


def fetch_eonet():
    out = []
    try:
        r = requests.get(
            "https://eonet.gsfc.nasa.gov/api/v3/events",
            params={"status": "open", "limit": 60},
            headers={"User-Agent": USER_AGENT}, timeout=12)
        for ev in r.json().get("events", []):
            cats = ev.get("categories", [])
            cid = cats[0].get("id") if cats else ""
            etype = EONET_CAT.get(cid, "other")
            geos = ev.get("geometry", [])
            if not geos:
                continue
            g = geos[-1]                       # most recent position
            coords = g.get("coordinates")
            try:
                if g.get("type") == "Point":
                    lon, lat = coords[0], coords[1]
                elif g.get("type") == "Polygon":
                    lon, lat = coords[0][0][0], coords[0][0][1]
                else:
                    continue
            except (TypeError, IndexError):
                continue
            out.append({"type": etype, "title": ev.get("title", "Event"),
                        "lat": lat, "lon": lon,
                        "detail": cats[0].get("title", "") if cats else "",
                        "time": g.get("date"), "source": "NASA EONET"})
    except Exception:
        pass
    return out


def get_disasters():
    now = time.time()
    if _DISASTER_CACHE["data"] is not None and (now - _DISASTER_CACHE["t"]) < 300:
        return _DISASTER_CACHE["data"]

    quakes = fetch_usgs()[:35]          # cap the earthquake firehose
    natural = fetch_eonet()[:35]        # fires / storms / floods / volcanoes
    print(f"[disasters] USGS={len(quakes)}  EONET={len(natural)}")

    # interleave so neither source dominates the visible list
    merged, i, j = [], 0, 0
    while i < len(natural) or j < len(quakes):
        if i < len(natural):
            merged.append(natural[i]); i += 1
        if j < len(quakes):
            merged.append(quakes[j]); j += 1

    _DISASTER_CACHE.update({"t": now, "data": merged})
    return merged


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
def build_gemini_contents(history, new_prompt):
    """Turn a frontend conversation history [{role, text}, ...] into Gemini's multi-turn contents shape."""
    contents = []
    for turn in (history or [])[-20:]:
        role = "model" if turn.get("role") == "model" else "user"
        text = (turn.get("text") or "").strip()
        if text:
            contents.append({"role": role, "parts": [{"text": text}]})
    contents.append({"role": "user", "parts": [{"text": new_prompt}]})
    return contents


def query_gemini(contents, system_instruction, api_key):
    if not api_key:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"
    payload = {"contents": contents}
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
    try:
        resp = requests.post(url, json=payload, timeout=20)
        if resp.status_code == 200:
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        pass
    return None


def geocode_address(query):
    if not query:
        return None

    cleaned = " ".join((query or "").split())
    if not cleaned:
        return None

    variants = []
    seen = set()
    for candidate in [
        cleaned,
        cleaned.replace(",", " "),
        cleaned.replace(" Dr", " Drive").replace(" Dr.", " Drive"),
        cleaned.replace(" St", " Street").replace(" St.", " Street"),
        cleaned.replace(" Rd", " Road").replace(" Rd.", " Road"),
        cleaned.replace(" Ave", " Avenue").replace(" Ave.", " Avenue"),
        cleaned.replace(" Blvd", " Boulevard").replace(" Blvd.", " Boulevard"),
    ]:
        if candidate and candidate not in seen:
            seen.add(candidate)
            variants.append(candidate)

    def parse_result(payload, display_name=None):
        if not isinstance(payload, dict):
            return None
        lat = payload.get("lat")
        lon = payload.get("lon")
        if lat is None or lon is None:
            return None
        try:
            return {"lat": float(lat), "lon": float(lon), "display_name": display_name or payload.get("display_name") or payload.get("name")}
        except (TypeError, ValueError):
            return None

    for candidate in variants:
        try:
            arcgis_url = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"
            arcgis_params = {"f": "json", "SingleLine": candidate}
            arcgis_resp = requests.get(arcgis_url, params=arcgis_params, headers={"User-Agent": USER_AGENT}, timeout=20)
            arcgis_resp.raise_for_status()
            arcgis_data = arcgis_resp.json()
            arcgis_candidates = arcgis_data.get("candidates", [])
            if arcgis_candidates:
                best = arcgis_candidates[0]
                loc = best.get("location", {})
                if loc.get("x") is not None and loc.get("y") is not None:
                    return {
                        "lat": float(loc["y"]),
                        "lon": float(loc["x"]),
                        "display_name": best.get("address", candidate),
                    }
        except Exception:
            pass

        try:
            nominatim_url = "https://nominatim.openstreetmap.org/search"
            nominatim_params = {"format": "json", "limit": 5, "addressdetails": 1, "q": candidate}
            nominatim_resp = requests.get(nominatim_url, params=nominatim_params, headers={"User-Agent": USER_AGENT}, timeout=20)
            nominatim_resp.raise_for_status()
            nominatim_data = nominatim_resp.json()
            if isinstance(nominatim_data, list) and nominatim_data:
                match = next((item for item in nominatim_data if item.get("lat") and item.get("lon")), nominatim_data[0])
                result = parse_result(match, match.get("display_name", candidate))
                if result:
                    return result
        except Exception:
            pass

        try:
            photon_url = "https://photon.komoot.io/api/"
            photon_params = {"q": candidate, "limit": 3}
            photon_resp = requests.get(photon_url, params=photon_params, headers={"User-Agent": USER_AGENT}, timeout=20)
            photon_resp.raise_for_status()
            photon_data = photon_resp.json()
            features = photon_data.get("features", [])
            if features:
                props = features[0].get("properties", {})
                coords = features[0].get("geometry", {}).get("coordinates", [])
                if len(coords) >= 2:
                    result = parse_result({"lat": coords[1], "lon": coords[0], "display_name": props.get("name") or candidate}, props.get("name") or candidate)
                    if result:
                        return result
        except Exception:
            pass

    return None


SYSTEM_PROMPT = (
    "You are the FinGeoRisk Actuarial AI Assistant, a conversational advisor embedded in the FinGeoRisk "
    "geospatial underwriting dashboard. Your purpose: help the user understand a selected property's "
    "catastrophe risk profile (flood, wind, wildfire), its financial underwriting metrics (insured value, "
    "annual premium, Estimated Maximum Loss, underwriting yield), and its property/market forecast (value, "
    "appreciation, rent, demand) — and suggest concrete, specific risk-mitigation actions when asked.\n\n"
    "You are given the exact data the user currently sees on screen for the selected property, refreshed on "
    "every message. Each risk vector and forecast figure is tagged with its source: real data (e.g. 'FEMA "
    "NFHL', 'USFS WHP', 'Open-Meteo', 'NASA FIRMS', 'FHFA House Price Index') or 'model' (an illustrative "
    "hash-based baseline used only where real data isn't available for that location). Always be explicit "
    "about that distinction when it's relevant — never present a model-estimated number as verified real data.\n\n"
    "This is a multi-turn conversation: build naturally on what was already discussed rather than repeating "
    "yourself. If the user asks what you can help with or how you work, answer directly and concretely using "
    "the current property's real data as a live example, rather than a generic disclaimer. Be concise, "
    "professional, and specific to the property at hand."
)


def offline_assessment(ctx):
    f = ctx.get("financials", {})
    v = ctx.get("vectors", {})
    if ctx.get("risk_summary"):
        return "[Offline Underwriting Intelligence] " + ctx["risk_summary"]
    return (
        f"[Offline Underwriting Intelligence] Asset value estimated at ${f.get('total_insured_value',0):,.2f} "
        f"with an Estimated Maximum Loss of {f.get('eml_pct',0)}% (${f.get('estimated_max_loss',0):,.2f}). "
        f"Target annual premium ${f.get('annual_premium',0):,.2f}; composite hazard index "
        f"{v.get('composite_idx',0)}/100; yield {f.get('underwriting_yield',0)}/10."
    )


def build_gemini_prompt(message, context):
    ctx = context or {}
    geo = ctx.get("geography", {})
    fin = ctx.get("financials", {})
    vec = ctx.get("vectors", {})
    vsrc = ctx.get("vector_sources") or {}
    fire = ctx.get("fire_activity") or {}
    forecast = ctx.get("forecast") or {}
    location_name = geo.get("name", "Selected Property")
    lat = geo.get("lat", 0)
    lon = geo.get("lon", 0)

    def src(key):
        return (vsrc.get(key) or {}).get("source", "model")

    lines = [
        f"Current on-screen data for {location_name} ({lat}, {lon}):",
        f"Risk tier: {ctx.get('tier', 'Unknown')}",
        f"Composite risk index: {vec.get('composite_idx', 0)}/100",
        f"Flood payout probability: {vec.get('flood_payout_prob', 0)}% (source: {src('flood')})",
        f"Wind payout probability: {vec.get('wind_payout_prob', 0)}% (source: {src('wind')})",
        f"Wildfire payout probability: {vec.get('wildfire_payout_prob', 0)}% (source: {src('wildfire')})",
    ]
    if fire.get("active"):
        lines.append(f"NASA FIRMS real-time alert: {fire.get('count', 0)} active fire detection(s) within ~50km in the last 3 days.")
    lines += [
        f"Total insured value: ${fin.get('total_insured_value', 0):,.0f}",
        f"Annual premium target: ${fin.get('annual_premium', 0):,.0f}",
        f"Estimated Maximum Loss: {fin.get('eml_pct', 0)}% (${fin.get('estimated_max_loss', 0):,.0f})",
        f"Underwriting yield rating: {fin.get('underwriting_yield', 0)}/10",
    ]
    if forecast:
        lines += [
            f"Property value estimate: ${forecast.get('property_value_estimate', 0):,.0f} (source: {forecast.get('value_source', 'model')})",
            f"5-year projected value: ${forecast.get('projected_5yr_value', 0):,.0f}",
            f"Appreciation rate: {forecast.get('appreciation_rate_pct', 0)}%/yr (source: {forecast.get('appreciation_source', 'model')})",
            f"Estimated monthly rent: ${forecast.get('monthly_rent_estimate', 0):,.0f}",
            f"Market outlook: {forecast.get('market_outlook', 'Unknown')}, {forecast.get('demand_level', 'Unknown')} demand",
        ]
    if ctx.get("risk_summary"):
        lines.append(f"Model-generated risk summary: {ctx['risk_summary']}")

    lines.append(f"\nUser message: {message}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(PROJECT_ROOT, "index.html")


@app.route("/frontend")
def frontend():
    return send_from_directory(PROJECT_ROOT, "index.html")


@app.route("/api/compute", methods=["POST"])
def api_compute():
    body = request.get_json(force=True) or {}
    lat = float(body.get("lat", 37.7749))
    lon = float(body.get("lon", -122.4194))
    name = body.get("name", "Selected Property Coordinate")
    return jsonify(compute_actuarial_metrics(lat, lon, name))


@app.route("/api/property-forecast", methods=["POST"])
def api_property_forecast():
    body = request.get_json(force=True) or {}
    lat = float(body.get("lat", 37.7749))
    lon = float(body.get("lon", -122.4194))
    name = body.get("name", "Selected Property Coordinate")
    address = body.get("address", "")
    return jsonify(estimate_property_finance(lat, lon, name, address))


@app.route("/api/simulate/flood", methods=["POST"])
def api_simulate_flood():
    body = request.get_json(force=True) or {}
    lat = float(body.get("lat", 37.7749))
    lon = float(body.get("lon", -122.4194))
    category = int(body.get("category", 3))
    return jsonify(simulate_flood(lat, lon, category))


@app.route("/api/simulate/wind-context", methods=["POST"])
def api_simulate_wind_context():
    body = request.get_json(force=True) or {}
    lat = float(body.get("lat", 37.7749))
    lon = float(body.get("lon", -122.4194))
    return jsonify(fetch_current_wind(lat, lon))


@app.route("/api/hotspots")
def api_hotspots():
    out = []
    for name, lat, lon in HOTSPOT_CITIES:
        m = compute_actuarial_metrics(lat, lon, name, live_overlays=False)
        out.append({"name": name, "lat": lat, "lon": lon,
                    "tier": m["tier"], "color": tier_color(m["tier"]),
                    "composite_idx": m["vectors"]["composite_idx"],
                    "vectors": m["vectors"]})
    return jsonify(out)


@app.route("/api/disasters")
def api_disasters():
    return jsonify({"events": get_disasters()})


@app.route("/api/geocode", methods=["POST"])
def api_geocode():
    body = request.get_json(force=True) or {}
    query = (body.get("query") or "").strip()
    result = geocode_address(query)
    if result:
        return jsonify({"ok": True, **result})
    return jsonify({"ok": False, "error": "No location found"})


@app.route("/api/avm-value")
def api_avm_value():
    address = (request.args.get("address") or "").strip()
    if not address:
        return jsonify({"ok": False, "error": "Address is required"}), 400

    cached = get_cached_avm(address)
    if cached:
        return jsonify({
            "ok": True,
            "source": "cache",
            "cache_hit": True,
            "price": cached["price"],
            "raw": cached.get("raw") or {},
        })

    live = fetch_live_avm(address)
    if live.get("ok"):
        return jsonify({
            "ok": True,
            "source": "live",
            "cache_hit": False,
            "price": live["price"],
            "raw": live.get("raw") or {},
        })

    return jsonify({
        "ok": False,
        "source": "unavailable",
        "status": live.get("status", 502),
        "error": live.get("error", "AVM request failed"),
    }), 502


@app.route("/api/property-profile")
def api_property_profile():
    address = (request.args.get("address") or "").strip()
    if not address:
        return jsonify({"ok": False, "error": "Address is required"}), 400

    baseline, source, provider_status = resolve_property_profile(address)

    return jsonify({
        "ok": True,
        "source": source,
        "provider_status": provider_status,
        "property_data": baseline,
    })


@app.route("/api/chat", methods=["POST"])
def api_chat():
    body = request.get_json(force=True) or {}
    message = (body.get("message") or "").strip()
    context = body.get("context") or {}
    history = body.get("history") or []
    api_key = (body.get("api_key") or "").strip() or ENV_API_KEY
    if not message:
        return jsonify({"reply": "Enter an actuarial or economic vector query.", "mode": "offline"})
    prompt = build_gemini_prompt(message, context)
    reply = query_gemini(build_gemini_contents(history, prompt), SYSTEM_PROMPT, api_key)
    if reply:
        return jsonify({"reply": reply, "mode": "live"})
    return jsonify({"reply": offline_assessment(context), "mode": "offline"})


@app.route("/api/ai-insights", methods=["POST"])
def api_ai_insights():
    body = request.get_json(force=True) or {}
    message = (body.get("message") or "Write a concise underwriting memo for this location.").strip()
    context = body.get("context") or {}
    history = body.get("history") or []
    api_key = (body.get("api_key") or "").strip() or ENV_API_KEY
    prompt = build_gemini_prompt(message, context)
    reply = query_gemini(build_gemini_contents(history, prompt), SYSTEM_PROMPT, api_key)
    if reply:
        return jsonify({"reply": reply, "mode": "live"})
    return jsonify({"reply": offline_assessment(context), "mode": "offline"})


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug_mode)