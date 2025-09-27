from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

app = Flask(__name__)
CORS(app)

UA = {"User-Agent": "uv-compare-demo/1.0 (mailto:example@example.com)"}

# Fallback states (used if Overpass is slow/unavailable)
FALLBACK_STATES = [
    "New South Wales", "Victoria", "Queensland", "South Australia",
    "Western Australia", "Tasmania", "Northern Territory",
    "Australian Capital Territory"
]

OVERPASS = "https://overpass-api.de/api/interpreter"
NOMINATIM = "https://nominatim.openstreetmap.org/search"

def overpass(query: str):
    r = requests.post(OVERPASS, data={"data": query}, headers=UA, timeout=45)
    r.raise_for_status()
    return r.json()

def bbox_to_viewbox(bbox):
    # west,south,east,north -> viewbox=left,top,right,bottom (Nominatim wants left,top,right,bottom)
    west, south, east, north = bbox
    return f"{west},{north},{east},{south}"

@app.get("/api/states")
def api_states():
    # Try Overpass (admin level 4 inside AU), else fallback list
    query = """
    [out:json][timeout:25];
    area["ISO3166-1"="AU"]->.au;
    rel(area.au)["boundary"="administrative"]["admin_level"="4"];
    out tags;
    """
    try:
        data = overpass(query)
        names = sorted({el["tags"].get("name") for el in data.get("elements", []) if el.get("tags", {}).get("name")})
        if names:
            return jsonify(names)
    except Exception:
        pass
    return jsonify(FALLBACK_STATES)

@app.get("/api/suburbs")
def api_suburbs():
    """Type-ahead suburbs within a state. params: state, q (optional), limit"""
    state = request.args.get("state", "").strip()
    q = request.args.get("q", "").strip()
    limit = int(request.args.get("limit", 50))

    if not state:
        return jsonify([])

    # Get state bbox via Overpass for better filtering
    bbox = None
    try:
        q_state = f"""
        [out:json][timeout:25];
        area["ISO3166-1"="AU"]->.au;
        rel(area.au)["boundary"="administrative"]["admin_level"="4"]["name"="{state}"];
        out bb;
        """
        state_res = overpass(q_state)
        if state_res.get("elements"):
            b = state_res["elements"][0]["bounds"]
            bbox = [b["minlon"], b["minlat"], b["maxlon"], b["maxlat"]]
    except Exception:
        bbox = None

    params = {
        "country": "Australia",
        "format": "jsonv2",
        "addressdetails": 1,
        "dedupe": 1,
        "limit": limit,
        "extratags": 0,
    }
    # Nominatim doesn't have a "state" filter param; use a query string plus viewbox
    # Build search phrase
    search_phrase = (q + " suburb " + state).strip() if q else ("suburb " + state)
    params["q"] = search_phrase

    if bbox:
        params["viewbox"] = bbox_to_viewbox(bbox)
        params["bounded"] = 1

    try:
        r = requests.get(NOMINATIM, params=params, headers=UA, timeout=30)
        r.raise_for_status()
        items = r.json()
        suburbs = []
        seen = set()
        for it in items:
            # Only keep suburb/locality-like features
            cls = it.get("class")
            typ = it.get("type")
            if cls in ("place", "boundary") and typ in ("suburb", "neighbourhood", "quarter", "village", "town", "city", "locality"):
                name = it.get("display_name")
                plain = it.get("name") or it.get("display_name", "").split(",")[0]
                key = (plain, round(float(it["lat"]), 5), round(float(it["lon"]), 5))
                if key not in seen:
                    seen.add(key)
                    suburbs.append({
                        "name": plain,
                        "display_name": name,
                        "lat": float(it["lat"]),
                        "lon": float(it["lon"]),
                    })
        return jsonify(suburbs[:limit])
    except Exception as e:
        return jsonify([])

def fetch_uv(lat: float, lon: float):
    # Current hour + today’s max using Open-Meteo (no key)
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "uv_index",
        "daily": "uv_index_max,uv_index_clear_sky_max",
        "forecast_days": 1,
        "timezone": "auto",
    }
    r = requests.get(url, params=params, headers=UA, timeout=30)
    r.raise_for_status()
    j = r.json()

    tz = j.get("timezone", "UTC")
    # current hour index
    now = datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%dT%H:00")
    hourly_times = j.get("hourly", {}).get("time", [])
    hourly_uv = j.get("hourly", {}).get("uv_index", [])
    current_uv = None
    if now in hourly_times:
        idx = hourly_times.index(now)
        current_uv = hourly_uv[idx]
    # fall back to last known hour if exact hour not present
    if current_uv is None and hourly_uv:
        current_uv = hourly_uv[-1]

    daily = j.get("daily", {})
    uv_max = (daily.get("uv_index_max") or [None])[0]
    clear_sky_max = (daily.get("uv_index_clear_sky_max") or [None])[0]

    return {
        "current_uv": current_uv,
        "uv_max_today": uv_max,
        "uv_max_clear_sky_today": clear_sky_max,
        "timezone": tz
    }

def uv_advice(uv):
    """Very simple WHO-style bands."""
    if uv is None:
        return "UV data not available."
    uv_val = float(uv)
    if uv_val < 3:
        return "Low: Minimal protection. Sunglasses if bright."
    if uv_val < 6:
        return "Moderate: Shade 11am–3pm, SPF30+ every 2 hours, hat."
    if uv_val < 8:
        return "High: Seek shade 10am–4pm, SPF50+, hat, long sleeves."
    if uv_val < 11:
        return "Very High: Avoid midday sun, reapply SPF50+ often."
    return "Extreme: Stay indoors if possible. Full protection required."

@app.get("/api/uv")
def api_uv():
    """Get UV for a given suburb query OR lat/lon"""
    lat = request.args.get("lat")
    lon = request.args.get("lon")
    query = request.args.get("q")
    state = request.args.get("state")

    if not (lat and lon):
        if not query:
            return jsonify({"error": "Provide lat/lon or q (suburb)"}), 400
        # Geocode suburb + state in AU
        params = {
            "q": f"{query}, {state or ''}, Australia",
            "format": "jsonv2",
            "limit": 1
        }
        r = requests.get(NOMINATIM, params=params, headers=UA, timeout=20)
        r.raise_for_status()
        res = r.json()
        if not res:
            return jsonify({"error": "Location not found"}), 404
        lat = float(res[0]["lat"])
        lon = float(res[0]["lon"])
    else:
        lat = float(lat); lon = float(lon)

    uv = fetch_uv(lat, lon)
    return jsonify({
        "lat": lat, "lon": lon,
        "current_uv": uv["current_uv"],
        "uv_max_today": uv["uv_max_today"],
        "uv_max_clear_sky_today": uv["uv_max_clear_sky_today"],
        "advice_now": uv_advice(uv["current_uv"]),
        "advice_max": uv_advice(uv["uv_max_today"]),
        "timezone": uv["timezone"]
    })

@app.get("/api/compare")
def api_compare():
    """
    Compare two suburbs. params:
    left_state, left_suburb, right_state, right_suburb
    """
    ls = request.args.get("left_state", "")
    lq = request.args.get("left_suburb", "")
    rs = request.args.get("right_state", "")
    rq = request.args.get("right_suburb", "")

    def one(state, q):
        if not q:
            return None
        params = {"q": f"{q}, {state}, Australia", "format": "jsonv2", "limit": 1}
        r = requests.get(NOMINATIM, params=params, headers=UA, timeout=20)
        r.raise_for_status()
        res = r.json()
        if not res:
            return None
        lat, lon = float(res[0]["lat"]), float(res[0]["lon"])
        uv = fetch_uv(lat, lon)
        return {
            "state": state, "suburb": q, "lat": lat, "lon": lon,
            "current_uv": uv["current_uv"],
            "uv_max_today": uv["uv_max_today"],
            "uv_max_clear_sky_today": uv["uv_max_clear_sky_today"],
            "advice_now": uv_advice(uv["current_uv"]),
            "advice_max": uv_advice(uv["uv_max_today"]),
            "timezone": uv["timezone"]
        }

    left = one(ls, lq)
    right = one(rs, rq)

    summary = ""
    if left and right:
        # Simple comparison summary
        def fmt(x): 
            return f"{x['suburb']}, {x['state']}"
        higher_now = (left if (left["current_uv"] or -1) >= (right["current_uv"] or -1) else right)
        higher_max = (left if (left["uv_max_today"] or -1) >= (right["uv_max_today"] or -1) else right)
        summary = (
            f"Right now, higher UV is in **{fmt(higher_now)}** "
            f"(UV {higher_now['current_uv']}). "
            f"Today’s max UV is expected to be higher in **{fmt(higher_max)}** "
            f"(UV {higher_max['uv_max_today']}).\n\n"
            "Recommendation: Plan outdoor work in the lower-UV suburb/time window. "
            "Use SPF50+, wide-brim hat, sunglasses; seek shade late morning to mid-afternoon."
        )

    return jsonify({"left": left, "right": right, "summary": summary})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050)

