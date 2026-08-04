"""Anime3rb Stream Addon v8.0.0

Stream-only addon. No catalogs, no episode cache.
Every stream request fetches LIVE from anime3rb API via curl_cffi, so an episode
released on the website appears in the addon immediately.

Supported incoming IDs:
  - IMDB   (Xperience / Cinemeta style):  tt1234567:SEASON:EPISODE   (series)
                                          tt1234567                  (movie)
  - Kitsu  (legacy):                      kitsu:XXXXX:EPISODE
                                          kitsu:XXXXX:SEASON:EPISODE

Mapping data (data/imdb_map.json, data/kitsu_map.json) maps a stable series
identity -> anime3rb series id. Episode availability is always fetched live.
For long single-series anime (One Piece, Conan, ...) IMDB season/episode is
converted to an absolute episode number using pre-baked TVDB offsets.
"""
import json
import os
import re
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

# ─── Maps ───
DATA_DIR = Path(__file__).parent / "data"
_kitsu_map: dict = {}
_imdb_series: dict = {}
_imdb_movies: dict = {}

# ─── Request Log (for debugging) ───
_request_log: deque = deque(maxlen=50)


def _load_maps():
    global _kitsu_map, _imdb_series, _imdb_movies
    kpath = DATA_DIR / "kitsu_map.json"
    if kpath.exists():
        _kitsu_map = json.load(open(kpath))
        print(f"[Map] Loaded {len(_kitsu_map)} Kitsu mappings")
    ipath = DATA_DIR / "imdb_map.json"
    if ipath.exists():
        m = json.load(open(ipath))
        _imdb_series = m.get("series", {})
        _imdb_movies = m.get("movies", {})
        print(f"[Map] Loaded {len(_imdb_series)} IMDB series, {len(_imdb_movies)} IMDB movies")
    # Manual overrides take precedence — used for titles the auto-mapper misses
    # (e.g. brand-new movies not yet in the open IMDB<->anime relations database).
    opath = DATA_DIR / "overrides.json"
    if opath.exists():
        o = json.load(open(opath))
        _imdb_series.update(o.get("series", {}))
        _imdb_movies.update(o.get("movies", {}))
        print(f"[Map] Applied overrides: {len(o.get('series', {}))} series, "
              f"{len(o.get('movies', {}))} movies")


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


def _series_info(series_id: str):
    try:
        data = api_call("get_series_info", f"&series_id={series_id}", timeout=30)
    except Exception as e:
        print(f"[Stream] API error for series {series_id}: {e}")
        return None
    if not data or "episodes" not in data:
        return None
    return data


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
    "version": "8.0.0",
    "name": "Anime3rb بث",
    "description": "روابط بث مباشرة من anime3rb — حلقات جديدة فوراً (IMDB + Kitsu)",
    "logo": "https://anime3rb.vip/favicon.ico",
    "resources": ["stream"],
    "types": ["series", "movie"],
    "idPrefixes": ["tt", "kitsu:"],
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
    if stremio_id.startswith("tt"):
        if content_type == "series":
            streams = _get_imdb_series_stream(stremio_id)
        elif content_type == "movie":
            streams = _get_imdb_movie_stream(stremio_id)
    elif stremio_id.startswith("kitsu:"):
        if content_type == "series":
            streams = _get_kitsu_series_stream(stremio_id)
        elif content_type == "movie":
            streams = _get_kitsu_movie_stream(stremio_id)

    return stremio_response({"streams": streams})


# ─── Episode helpers ───
def _ep_int(ep_num_raw) -> int:
    """Leading integer of an episode_num (handles '132 ~ 134')."""
    m = re.match(r"\s*(\d+)", str(ep_num_raw))
    return int(m.group(1)) if m else 0


def _episodes_in_order(data: dict) -> list:
    """Flatten all seasons into one list ordered by episode number."""
    out = []
    eps = data["episodes"]
    for s_key in sorted(eps.keys(), key=lambda x: int(x)):
        out.extend(eps[s_key])
    out.sort(key=lambda ep: _ep_int(ep.get("episode_num", 0)))
    return out


def _find_by_epnum(data: dict, episode: int):
    """Find an episode by its anime3rb episode_num (handles combined ranges)."""
    eps = data["episodes"]
    for s_key in sorted(eps.keys(), key=lambda x: int(x)):
        for ep in eps[s_key]:
            raw = str(ep.get("episode_num", "0"))
            try:
                if "~" in raw:
                    a, b = raw.split("~")
                    if int(a.strip()) <= episode <= int(b.strip()):
                        return ep
                elif int(_ep_int(raw)) == episode:
                    return ep
            except (ValueError, IndexError):
                continue
    return None


def _build_stream(ep: dict) -> list:
    """Build Stremio stream object from episode data."""
    if not ep:
        return []
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


# ─── IMDB resolution ───
def _get_imdb_series_stream(stremio_id: str) -> list:
    """tt1234567:SEASON:EPISODE  ->  anime3rb stream (fetched live)."""
    parts = stremio_id.split(":")
    imdb = parts[0]
    try:
        season = int(parts[1]) if len(parts) > 1 else 1
        episode = int(parts[2]) if len(parts) > 2 else 1
    except (ValueError, IndexError):
        return []

    rec = _imdb_series.get(imdb)
    if not rec:
        print(f"[Stream] No IMDB mapping for {imdb}")
        return []

    season_parts = rec.get("seasons", {}).get(str(season))
    if season_parts:
        # Clean season mapping: one or more anime3rb series make up this season.
        if len(season_parts) == 1:
            data = _series_info(season_parts[0])
            if not data:
                return []
            return _build_stream(_find_by_epnum(data, episode))
        # Multiple parts within one IMDB season -> continuous numbering across parts.
        target = episode
        for a3 in season_parts:
            data = _series_info(a3)
            if not data:
                return []
            eps = _episodes_in_order(data)
            if target <= len(eps):
                return _build_stream(eps[target - 1])
            target -= len(eps)
        return []

    # Single long series (IMDB splits into many seasons, anime3rb uses absolute numbering).
    offsets = rec.get("season_offsets", {})
    if str(season) in offsets:
        abs_ep = offsets[str(season)] + episode
    else:
        abs_ep = episode  # fallback (usually season 1 / no offset data)

    ordered = rec.get("ordered", [])
    if len(ordered) == 1:
        data = _series_info(ordered[0])
        if not data:
            return []
        ep = _find_by_epnum(data, abs_ep)
        # Some catalogs already send an absolute episode number (e.g. One Piece
        # season 16 episode 643). If the season-offset conversion overshoots, fall
        # back to treating the given episode number as already-absolute.
        if not ep and abs_ep != episode:
            ep = _find_by_epnum(data, episode)
        return _build_stream(ep)

    # Messy multi-series without clean season map: concatenate by count.
    target = abs_ep
    for a3 in ordered:
        data = _series_info(a3)
        if not data:
            return []
        eps = _episodes_in_order(data)
        if target <= len(eps):
            return _build_stream(eps[target - 1])
        target -= len(eps)
    return []


def _get_imdb_movie_stream(stremio_id: str) -> list:
    imdb = stremio_id.split(":")[0]
    rec = _imdb_movies.get(imdb)
    if not rec:
        print(f"[Stream] No IMDB movie mapping for {imdb}")
        return []
    return _vod_or_series_stream(rec.get("id"), rec.get("type"))


# ─── Kitsu resolution (legacy) ───
def _get_kitsu_series_stream(stremio_id: str) -> list:
    parts = stremio_id.split(":")
    if len(parts) < 3:
        return []
    kitsu_id = parts[1]
    try:
        episode = int(parts[2]) if len(parts) == 3 else int(parts[3])
    except (ValueError, IndexError):
        return []
    mapping = _kitsu_map.get(kitsu_id)
    if not mapping:
        print(f"[Stream] No mapping for kitsu:{kitsu_id}")
        return []
    series_id = mapping.get("id") if isinstance(mapping, dict) else str(mapping)
    if not series_id:
        return []
    data = _series_info(series_id)
    if not data:
        return []
    ep = _find_by_epnum(data, episode)
    if not ep:
        print(f"[Stream] Episode {episode} not found in series {series_id} (kitsu:{kitsu_id})")
    return _build_stream(ep)


def _get_kitsu_movie_stream(stremio_id: str) -> list:
    parts = stremio_id.split(":")
    if len(parts) < 2:
        return []
    mapping = _kitsu_map.get(parts[1])
    if not mapping:
        return []
    item_type = mapping.get("type") if isinstance(mapping, dict) else "series"
    item_id = mapping.get("id") if isinstance(mapping, dict) else str(mapping)
    return _vod_or_series_stream(item_id, item_type)


def _vod_or_series_stream(item_id, item_type) -> list:
    if not item_id:
        return []
    if item_type == "vod":
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
    # series-type "movie" (a movie stored as a single-episode series)
    data = _series_info(item_id)
    if not data:
        return []
    eps = _episodes_in_order(data)
    return _build_stream(eps[0]) if eps else []


# ─── Health / Debug ───
@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": MANIFEST["version"],
        "kitsu_map_size": len(_kitsu_map),
        "imdb_series_size": len(_imdb_series),
        "imdb_movies_size": len(_imdb_movies),
        "mode": "stream-only, live from source, no cache",
    }


@app.get("/debug/requests")
def debug_requests():
    return {"requests": list(_request_log)}


@app.on_event("startup")
def startup():
    _load_maps()
    print(f"[Addon] Anime3rb Stream v{MANIFEST['version']} — LIVE from source, no cache!")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
