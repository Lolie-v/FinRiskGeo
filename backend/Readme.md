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

## Environment variables

The backend uses the following environment variables:

- `GEMINI_API_KEY`: optional Google Gemini API key
- `GEMINI_MODEL`: optional model name, defaults to `gemini-2.5-flash`
- `RENTCAST_API_KEY`: optional RentCast API key, used for real property valuations
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
- `GEMINI_MODEL` (optional, defaults to `gemini-2.5-flash`)

Render injects its own `PORT` env var, which the Gunicorn start command binds to directly.

After saving, trigger a manual deploy ("Deploy latest commit") or push to `main` to redeploy automatically.

## Notes

This is a demonstration application for geospatial underwriting concepts. The financial outputs (premium, EML, yield) are still illustrative, deterministic calculations rather than a real insurer's proprietary pricing model — but where free public data is available (FEMA flood zones, USFS wildfire hazard, historical wind extremes, FHFA home price trends), the underlying hazard/appreciation inputs are real rather than synthetic. Coverage is generally US-only; non-US locations and any source that fails or lacks coverage fall back to the deterministic baseline. The goal is to showcase how geospatial analytics, real public hazard data, and AI-assisted underwriting can be presented in a polished local dashboard experience.
