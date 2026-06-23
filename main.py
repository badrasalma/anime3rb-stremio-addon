"""Anime3rb Stream Addon v7.0.0

Stream-only addon. No catalogs, no cache for episodes.
Every stream request fetches LIVE from anime3rb API via curl_cffi.
Episode released on the website = appears in the addon immediately.

Uses Kitsu ID mapping (kitsu:XXXX → anime3rb series_id).
"""
import json
import os
import time
from collections import deque
from pathlib import Path

from curl_cffi import requests as cffi_requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ─── Configuration ───
BASE_URL = os.environ.get("ANIME3RB_URL", "https://anime3rb.vip")
USERNAME = os.environ.get("ANIME3RB_USER", "bbnmnbb")
PASSWORD = os.environ.get("ANIME3RB_PASS", "as209509")

# ─── Kitsu ID Map ───
DATA_DIR = Path(__file__).parent / "data"
_kitsu_map: dict = {}

# ─── Request Log (for debugging) ───
_request_log: deque = deque(maxlen=50)


def _load_kitsu_map():
    global _kitsu_map
    fpath = DATA_DIR / "kitsu_map.json"
    if fpath.exists():
        with open(fpath) as f:
            _kitsu_map = json.load(f)
        print(f"[Map] Loaded {len(_kitsu_map)} Kitsu mappings")
    else:
        print("[Map] WARNING: kitsu_map.json not found!")


# ─── API Call (curl_cffi — bypasses Cloudflare) ───
def api_call(action: str = "", extra: str = "", timeout: int = 60):
    url = f"{BASE_URL}/player_api.php?username={USERNAME}&password={PASSWORD}"
    if action:
        url += f"&action={action}"
    if extra:
        url += extra
    r = cffi_requests.get(url, impersonate="chrome", timeout=timeout)
    if r.status_code == 200:
        return r.json()
    raise RuntimeError(f"API returned {r.status_code}")


# ─── FastAPI App ───
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def stremio_response(data):
    from fastapi.responses import JSONResponse
    return JSONResponse(
        content=data,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


# ─── Manifest ───
MANIFEST = {
    "id": "com.anime3rb.stream",
    "version": "7.0.0",
    "name": "Anime3rb بث",
    "description": "روابط بث مباشرة من anime3rb — حلقات جديدة فوراً",
    "logo": "https://anime3rb.vip/favicon.ico",
    "resources": ["stream"],
    "types": ["series", "movie"],
    "idPrefixes": ["kitsu:"],
    "catalogs": [],
    "behaviorHints": {"configurable": False},
}


@app.get("/manifest.json")
@app.get("/{params}/manifest.json")
def manifest(params: str = ""):
    return stremio_response(MANIFEST)


# ─── Stream Handler ───
@app.get("/stream/{content_type}/{stremio_id}.json")
@app.get("/{params}/stream/{content_type}/{stremio_id}.json")
def stream(content_type: str, stremio_id: str, params: str = ""):
    """Fetch stream LIVE from anime3rb API. No cache."""
    print(f"[Stream] Request: {content_type}/{stremio_id}")
    _request_log.append({
        "time": time.strftime("%H:%M:%S"),
        "type": content_type,
        "id": stremio_id,
    })

    streams = []

    if content_type == "series":
        streams = _get_series_stream(stremio_id)
    elif content_type == "movie":
        streams = _get_movie_stream(stremio_id)

    return stremio_response({"streams": streams})


def _get_series_stream(stremio_id: str) -> list:
    """Get stream for a series episode.
    
    stremio_id format: kitsu:XXXXX:SEASON:EPISODE
    """
    parts = stremio_id.split(":")
    if len(parts) < 4 or parts[0] != "kitsu":
        return []

    kitsu_id = parts[1]
    try:
        season = int(parts[2])
        episode = int(parts[3])
    except (ValueError, IndexError):
        return []

    # Look up anime3rb series_id from kitsu map
    mapping = _kitsu_map.get(kitsu_id)
    if not mapping:
        print(f"[Stream] No mapping for kitsu:{kitsu_id}")
        return []

    series_id = mapping.get("id") if isinstance(mapping, dict) else str(mapping)
    if not series_id:
        return []

    # Fetch episodes LIVE from API
    try:
        data = api_call("get_series_info", f"&series_id={series_id}", timeout=30)
    except Exception as e:
        print(f"[Stream] API error for series {series_id}: {e}")
        return []

    if not data or "episodes" not in data:
        return []

    # Find the episode
    episodes_data = data["episodes"]
    
    # Try exact season match first
    season_key = str(season)
    if season_key in episodes_data:
        for ep in episodes_data[season_key]:
            ep_num = int(ep.get("episode_num", 0))
            if ep_num == episode:
                return _build_stream(ep)

    # If season 1, also search all seasons (some anime have only season 1)
    if season == 1:
        for s_key, eps in episodes_data.items():
            for ep in eps:
                ep_num = int(ep.get("episode_num", 0))
                if ep_num == episode:
                    return _build_stream(ep)

    # Fallback: search by absolute episode number across all seasons
    absolute_ep = episode
    for s_key in sorted(episodes_data.keys(), key=lambda x: int(x)):
        for ep in episodes_data[s_key]:
            ep_num = int(ep.get("episode_num", 0))
            if ep_num == absolute_ep:
                return _build_stream(ep)

    print(f"[Stream] Episode not found: S{season}E{episode} in series {series_id}")
    return []


def _build_stream(ep: dict) -> list:
    """Build Stremio stream object from episode data."""
    stream_id = ep.get("id") or ep.get("stream_id")
    container = ep.get("container_extension", "mp4")
    if not stream_id:
        return []
    
    url = f"{BASE_URL}/series/{USERNAME}/{PASSWORD}/{stream_id}.{container}"
    title = ep.get("title") or f"Episode {ep.get('episode_num', '?')}"
    
    return [{
        "url": url,
        "title": f"Anime3rb: {title}",
        "behaviorHints": {"notWebReady": True},
    }]


def _get_movie_stream(stremio_id: str) -> list:
    """Get stream for a movie.
    
    stremio_id format: kitsu:XXXXX
    """
    parts = stremio_id.split(":")
    if len(parts) < 2 or parts[0] != "kitsu":
        return []

    kitsu_id = parts[1]
    mapping = _kitsu_map.get(kitsu_id)
    if not mapping:
        return []

    item_type = mapping.get("type") if isinstance(mapping, dict) else "series"
    item_id = mapping.get("id") if isinstance(mapping, dict) else str(mapping)

    if item_type == "vod" and item_id:
        try:
            data = api_call("get_vod_info", f"&vod_id={item_id}", timeout=30)
        except Exception as e:
            print(f"[Stream] API error for vod {item_id}: {e}")
            return []
        
        if data and "movie_data" in data:
            movie = data["movie_data"]
            container = movie.get("container_extension", "mp4")
            stream_id = movie.get("stream_id", item_id)
            url = f"{BASE_URL}/movie/{USERNAME}/{PASSWORD}/{stream_id}.{container}"
            name = data.get("info", {}).get("name") or movie.get("name", "Movie")
            return [{
                "url": url,
                "title": f"Anime3rb: {name}",
                "behaviorHints": {"notWebReady": True},
            }]

    return []


# ─── Health ───
@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "7.0.0",
        "kitsu_map_size": len(_kitsu_map),
        "mode": "stream-only, live from source, no cache",
    }


@app.get("/debug/requests")
def debug_requests():
    """Show last 50 stream requests for debugging."""
    return {"requests": list(_request_log)}


# ─── Startup ───
@app.on_event("startup")
def startup():
    _load_kitsu_map()
    print("[Addon] Anime3rb Stream v7.0.0 — LIVE from source, no cache!")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
