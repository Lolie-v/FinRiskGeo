# FinGeoRisk Backend

FinGeoRisk is an actuarial geospatial demo that combines a deterministic underwriting engine, an interactive map interface, and Gemini-assisted analysis to explore catastrophe risk for specific locations. The project now works as a complete local web application: the Flask backend serves the frontend automatically, the map visualizes risk exposure, and the UI supports guided underwriting workflows.

## What this implementation does

### 1. Actuarial risk engine
The backend calculates location-based underwriting metrics for any latitude/longitude pair, producing:

- total insured value
- annual premium target
- estimated maximum loss (EML)
- flood, wind, and wildfire payout probabilities
- a composite risk index
- a risk tier such as Minimal, Moderate, High Exposure, or Critical Exposure
- a plain-language risk summary explaining why the location is considered risky

The underlying model is a deterministic baseline rather than a real insurer's proprietary catastrophe model, but for US locations the flood, wildfire, and wind vectors are overlaid with real free public data where coverage exists, falling back to the deterministic estimate otherwise:

- **Flood** — FEMA's National Flood Hazard Layer (real flood zone at the exact point)
- **Wildfire** — USFS Wildfire Hazard Potential (real national hazard classification)
- **Wind/hurricane** — Open-Meteo's historical weather archive (real 10-year max wind gust at the point)
- **Home price appreciation** (in the property forecast) — FHFA's state House Price Index (real year-over-year trend, downloaded directly from FHFA, no key required)

Each response includes a `vector_sources` (or `appreciation_source`/`appreciation_status`) field reporting whether a number came from real data or the model fallback. This logic is implemented in `compute_actuarial_metrics` and `estimate_property_finance` in `app.py`. The `/api/hotspots` overview endpoint intentionally skips the live overlays (it iterates many cities at once) and always uses the fast deterministic baseline.

Additionally, when `NASA_FIRMS_MAP_KEY` is configured, `/api/compute` includes a supplementary `fire_activity` field reporting whether NASA FIRMS has detected any active fires within ~50km of the point in the last 3 days. This is a real-time signal, not a long-term hazard score — it's shown separately from `wildfire_payout_prob` and never overrides it.

### 2. Geocoding and location resolution
The app can resolve user-entered addresses, cities, ZIP codes, and other place names into latitude/longitude coordinates. It uses multiple geocoding providers for robustness:

- ArcGIS geocoding first
- Nominatim fallback when needed

This makes the search workflow more reliable for both simple and detailed address inputs.

### 3. Interactive map and risk visualization
The frontend uses a satellite-style map to visualize:

- selected target locations
- color-coded hotspot markers for major risk areas
- shaded disaster-exposure zones for wildfire, hurricane, tornado, and flood-prone regions
- live disaster event markers sourced from public feeds

Users can also toggle individual overlay layers on or off, and multiple overlays can be shown at the same time.

### 4. Live disaster monitoring
The backend fetches and caches disaster information from:

- USGS earthquake feed
- NASA EONET events feed

These are merged into a single live disaster stream and returned by the `/api/disasters` endpoint.

### 5. Gemini AI integration
The backend integrates with Google Gemini to support underwriting assistance. The current capabilities include:

- answering natural-language questions through `/api/chat`
- generating concise underwriting memos through `/api/ai-insights`
- using the current location, insured value, premium target, EML, and hazard metrics as structured context for Gemini prompts

If no Gemini API key is supplied, the app falls back to an offline underwriting assessment so the experience still works.

### 6. Frontend experience improvements
The frontend has been enhanced with several usability features:

- a visible theme picker that changes the dashboard background color
- a persistent location history panel that stores recent searches and brings users back to earlier views
- a sidebar control panel for disaster overlays and location lookup
- automatic frontend serving from the backend so the app opens directly from Flask

### 7. Disaster scenario simulator ("Simulation" tab)
A 3D-terrain scenario tool: click anywhere on the map to place a disaster origin, pick a hazard type and intensity, and see the affected area, peak intensity, and an illustrative dollar-loss estimate for the currently selected property. This is explicitly a **what-if scenario tool, not a certified catastrophe model** — every number is either real data or a named published reference formula, never arbitrary:

- **Flood (storm surge)**: real — samples a grid of real elevation points around the origin (Open-Meteo Elevation API) and floods cells at or below the surge height for the chosen hurricane category (NOAA's typical surge-height ranges). Inland/high-elevation origins correctly show "not applicable" rather than a fake flood.
- **Wildfire spread**: real current wind speed/direction at the origin (Open-Meteo forecast) drives the direction and shape of a simplified fire-spread ellipse; the spread-rate/ellipse-ratio formula itself is an illustrative approximation, not a fuel-model (e.g. Rothermel) simulation.
- **Hurricane wind field**: a simplified wind-radii decay model using real Saffir-Simpson category wind thresholds, mirroring the shape of NOAA's own wind-radii convention (34/50/64kt rings).
- **Tornado path**: NOAA's published average path length/width statistics by EF-scale rating, rendered as a damage swath along an illustrative track bearing.
- **Earthquake shaking**: a simplified magnitude-distance MMI attenuation formula (GMICE-style), rendered as concentric shaking-intensity rings.

Every hazard's damage-ratio curve (flood depth-damage, wind vulnerability, EF-scale, MMI vulnerability) is a simplified version of a published methodology (FEMA/USACE, HAZUS-style), and the dollar-loss figure is always anchored to the currently selected property's real total insured value — never a fabricated city-wide total.

## API endpoints

### `/`
Serves the main frontend HTML page.

### `/frontend`
Also serves the frontend HTML page for convenience.

### `/api/compute`
Accepts a JSON payload with `lat`, `lon`, and `name`, then returns actuarial metrics for that location.

### `/api/hotspots`
Returns hotspot locations and their risk scores for display on the map.

### `/api/disasters`
Returns a merged feed of live disaster events.

### `/api/geocode`
Accepts a search query and returns geocoded coordinates and a display name.

### `/api/chat`
Accepts a message and current risk context, then returns either a Gemini-generated answer or an offline fallback assessment.

### `/api/ai-insights`
Generates a concise underwriting memo using the selected location and computed risk context.

### `/api/simulate/flood`
Accepts `lat`, `lon`, and `category` (1-5), returns a real-elevation-grid storm-surge flood simulation.

### `/api/simulate/wind-context`
Accepts `lat` and `lon`, returns real current wind speed/direction at that point (feeds the wildfire spread simulation).

## Environment variables

The backend uses the following environment variables:

- `GEMINI_API_KEY`: optional Google Gemini API key
- `GEMINI_MODEL`: optional model name, defaults to `gemini-2.5-flash`
- `RENTCAST_API_KEY`: optional RentCast API key, used for real property valuations
- `NASA_FIRMS_MAP_KEY`: optional free key from `firms.modaps.eosdis.nasa.gov/api/area/`, used for the supplementary active-fire-nearby badge. Without it, that badge is simply hidden.
- FEMA NFHL, USFS Wildfire Hazard Potential, Open-Meteo's historical archive, and FHFA's House Price Index need no API keys.

## Running the app

From the project root or the `backend` directory, install dependencies and start the Flask app:

```bash
cd backend
pip install -r requirements.txt
python app.py
```

Then open:

```text
http://127.0.0.1:5000
```

The Flask app will automatically serve the frontend from the same local server.

## Deploying to Render

Live deployment: https://finriskgeo.onrender.com/

The repo includes a `render.yaml` at the project root that points Render at the `backend/` folder. If your Render service was created from the dashboard (not from the Blueprint), it won't pick up `render.yaml` automatically — set these fields manually under the service's **Settings**:

- **Build Command**: `pip install -r backend/requirements.txt`
- **Start Command**: `gunicorn --chdir backend app:app --bind 0.0.0.0:$PORT`
- **Root Directory**: leave blank (both commands above are relative to the repo root)

Then, under the **Environment** tab, add the secrets that normally live in `backend/.env` (that file is gitignored and never reaches Render):

- `GEMINI_API_KEY`
- `RENTCAST_API_KEY`
- `NASA_FIRMS_MAP_KEY` (free, from `firms.modaps.eosdis.nasa.gov/api/area/`) — without it, the active-fire badge is simply hidden
- `GEMINI_MODEL` (optional, defaults to `gemini-2.5-flash`)

Render injects its own `PORT` env var, which the Gunicorn start command binds to directly.

After saving, trigger a manual deploy ("Deploy latest commit") or push to `main` to redeploy automatically.

## Notes

This is a demonstration application for geospatial underwriting concepts. The financial outputs (premium, EML, yield) are still illustrative, deterministic calculations rather than a real insurer's proprietary pricing model.

**Real data coverage, by vector:**
- **Global**: wind/hurricane risk (Open-Meteo historical wind gust) and the active-fire-nearby badge (NASA FIRMS) use real data anywhere in the world.
- **US-only**: flood zone (FEMA NFHL), wildfire hazard class (USFS WHP), and home price appreciation (FHFA HPI) only have real data inside the United States. Outside the US — or if a live source fails — these fall back to the illustrative hash-based baseline. The UI always labels which is which (e.g. "FEMA NFHL (US)" for real data vs. "MODEL (outside US coverage)" for the fallback), so it's never presented as more accurate than it is.

We looked for free global equivalents for the US-only vectors and didn't find reliable ones: Open-Meteo's global flood API exists but is unreliable for point queries (e.g. it under-reports discharge at major river confluences and misses tidal/storm-surge flooding entirely), global wildfire hazard datasets are only available as map tiles rather than a point-queryable API, and there's no free API with true worldwide home price coverage. Rather than wire in a technically-real-but-misleading number, those three stay clearly labeled as model estimates outside the US.
