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
import importlib
import requests
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
# Actuarial model
# ---------------------------------------------------------------------------
def compute_actuarial_metrics(lat, lon, location_name="Selected Property"):
    """Deterministic hyper-local cat-model -> financial underwriting parameters."""
    seed = abs(hash(f"{lat:.3f},{lon:.3f}"))

    coastal = any(w in location_name.lower() for w in ("water", "coast", "beach", "bay", "harbor", "port"))
    base_flood = (seed % 75) + 10 if coastal else (seed % 55) + 5
    base_wind = ((seed >> 2) % 65) + 15
    base_wildfire = ((seed >> 4) % 80) + 5 if base_flood < 30 else (seed % 25)

    base_flood = clip(base_flood, 0, 100)
    base_wind = clip(base_wind, 0, 100)
    base_wildfire = clip(base_wildfire, 0, 100)

    total_insured_value = 1250000 + ((seed % 500) * 5000)
    composite_risk_idx = (base_flood * 0.45) + (base_wind * 0.35) + (base_wildfire * 0.20)
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
    perils = {"flood": base_flood, "hurricane/wind": base_wind, "wildfire": base_wildfire}
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
            "flood_payout_prob": round(base_flood, 1),
            "wind_payout_prob": round(base_wind, 1),
            "wildfire_payout_prob": round(base_wildfire, 1),
            "composite_idx": round(composite_risk_idx, 1),
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
def query_gemini(prompt, system_instruction, api_key):
    if not api_key:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
    try:
        resp = requests.post(url, json=payload, timeout=15)
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
    "You are the FinGeoRisk Actuarial AI Assistant. You interpret financial-geospatial risk models.\n"
    "CRITICAL PROTOCOL: Explain the relationship between disaster probability, Estimated Maximum Loss (EML), "
    "and premium payouts based on the financial JSON context provided. Be concise and professional."
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
    location_name = geo.get("name", "Selected Property")
    lat = geo.get("lat", 0)
    lon = geo.get("lon", 0)
    return (
        "You are an actuarial underwriting assistant. Use the provided financial-geospatial context to answer the user's request. "
        "Be concise, professional, and actionable.\n\n"
        f"Location: {location_name} ({lat}, {lon})\n"
        f"Insured value: ${fin.get('total_insured_value', 0):,.0f}\n"
        f"Annual premium target: ${fin.get('annual_premium', 0):,.0f}\n"
        f"Estimated Maximum Loss: {fin.get('eml_pct', 0)}% (${fin.get('estimated_max_loss', 0):,.0f})\n"
        f"Yield rating: {fin.get('underwriting_yield', 0)}/10\n"
        f"Flood risk: {vec.get('flood_payout_prob', 0)}%\n"
        f"Wind risk: {vec.get('wind_payout_prob', 0)}%\n"
        f"Wildfire risk: {vec.get('wildfire_payout_prob', 0)}%\n"
        f"Composite risk index: {vec.get('composite_idx', 0)}\n\n"
        f"User request: {message}"
    )


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


@app.route("/api/hotspots")
def api_hotspots():
    out = []
    for name, lat, lon in HOTSPOT_CITIES:
        m = compute_actuarial_metrics(lat, lon, name)
        out.append({"name": name, "lat": lat, "lon": lon,
                    "tier": m["tier"], "color": tier_color(m["tier"]),
                    "composite_idx": m["vectors"]["composite_idx"]})
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
    api_key = (body.get("api_key") or "").strip() or ENV_API_KEY
    if not message:
        return jsonify({"reply": "Enter an actuarial or economic vector query.", "mode": "offline"})
    prompt = build_gemini_prompt(message, context)
    reply = query_gemini(prompt, SYSTEM_PROMPT, api_key)
    if reply:
        return jsonify({"reply": reply, "mode": "live"})
    return jsonify({"reply": offline_assessment(context), "mode": "offline"})


@app.route("/api/ai-insights", methods=["POST"])
def api_ai_insights():
    body = request.get_json(force=True) or {}
    message = (body.get("message") or "Write a concise underwriting memo for this location.").strip()
    context = body.get("context") or {}
    api_key = (body.get("api_key") or "").strip() or ENV_API_KEY
    prompt = build_gemini_prompt(message, context)
    reply = query_gemini(prompt, SYSTEM_PROMPT, api_key)
    if reply:
        return jsonify({"reply": reply, "mode": "live"})
    return jsonify({"reply": offline_assessment(context), "mode": "offline"})


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug_mode)