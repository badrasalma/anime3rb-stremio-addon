"""Anime3rb Stremio Addon — Stream-only (Kitsu ID based).

Provides anime stream links from anime3rb.vip subscription.
Uses a pre-built Kitsu ID → anime3rb series_id mapping for fast lookups.
No catalogs — use AIOMetadata/Kitsu for browsing.
"""
import json
import os
import sys
import time
import threading
from collections import OrderedDict
from typing import Any
from pathlib import Path

import requests
try:
    import cloudscraper
except ImportError:
    cloudscraper = None
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

# ─── Configuration ───
BASE_URL = os.environ.get("ANIME3RB_URL", "https://anime3rb.vip")
USERNAME = os.environ.get("ANIME3RB_USER", "bbnmnbb")
PASSWORD = os.environ.get("ANIME3RB_PASS", "as209509")

GITHUB_DATA_URL = os.environ.get(
    "GITHUB_DATA_URL",
    "https://raw.githubusercontent.com/badrasalma/anime3rb-stremio-addon/devin/deploy/data"
)
USE_CACHED = os.environ.get("USE_CACHED", "0") == "1"

# ─── Cloudscraper session ───
scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "linux"}) if cloudscraper else None

# ─── Cache ───
CACHE_TTL = 6 * 60 * 60
SERIES_INFO_TTL = 12 * 60 * 60
MAX_CACHE_MB = 60

_cache: OrderedDict[str, Any] = OrderedDict()
_cache_ts: dict[str, float] = {}
_lock = threading.Lock()


def _cache_size_mb() -> float:
    total = 0
    for v in _cache.values():
        try:
            total += sys.getsizeof(json.dumps(v, ensure_ascii=False))
        except (TypeError, ValueError):
            total += sys.getsizeof(v)
    return total / (1024 * 1024)


def _evict_if_needed() -> None:
    while _cache_size_mb() > MAX_CACHE_MB and _cache:
        key = next(iter(_cache))
        _cache.pop(key, None)
        _cache_ts.pop(key, None)


def cache_get(key: str, ttl: int | None = None) -> Any | None:
    with _lock:
        t = ttl if ttl is not None else CACHE_TTL
        if key in _cache and time.time() - _cache_ts.get(key, 0) < t:
            _cache.move_to_end(key)
            return _cache[key]
    return None


def cache_set(key: str, val: Any) -> None:
    with _lock:
        _cache[key] = val
        _cache_ts[key] = time.time()
        _cache.move_to_end(key)
        _evict_if_needed()


# ─── Pre-cached data ───
_cached_episodes: dict = {}  # series_id -> episode data
_kitsu_map: dict[str, str] = {}  # kitsu_id -> anime3rb series_id
_cached_data_ts: float = 0
CACHED_DATA_TTL = 6 * 60 * 60


def _load_github_json(filename: str) -> Any:
    url = f"{GITHUB_DATA_URL}/{filename}"
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[GitHub] Failed to load {filename}: {e}")
        return None


def _load_local_json(filename: str) -> Any:
    data_dir = Path(__file__).parent / "data"
    fpath = data_dir / filename
    if fpath.exists():
        with open(fpath) as f:
            return json.load(f)
    return None


def _load_cached_data() -> None:
    """Load pre-cached episode data and Kitsu mapping."""
    global _cached_episodes, _cached_data_ts, _kitsu_map
    if time.time() - _cached_data_ts < CACHED_DATA_TTL and _cached_episodes:
        return

    print("[Cache] Loading pre-cached data...")
    for loader_name, loader in [("local", _load_local_json), ("GitHub", _load_github_json)]:
        eps = loader("episodes.json")
        if eps and isinstance(eps, dict) and len(eps) > 0:
            _cached_episodes = eps
            _cached_data_ts = time.time()
            print(f"[Cache] Loaded {len(eps)} series episode data from {loader_name}")
            break
    else:
        print("[Cache] WARNING: No pre-cached episode data available")

    for loader_name, loader in [("local", _load_local_json), ("GitHub", _load_github_json)]:
        kmap = loader("kitsu_map.json")
        if kmap and isinstance(kmap, dict):
            _kitsu_map = kmap
            print(f"[Cache] Loaded {len(kmap)} Kitsu ID mappings from {loader_name}")
            break


# ─── Xtream API ───
def api_call(action: str = "", extra: str = "", timeout: int = 60) -> Any:
    url = f"{BASE_URL}/player_api.php?username={USERNAME}&password={PASSWORD}"
    if action:
        url += f"&action={action}"
    if extra:
        url += extra
    try:
        if scraper:
            r = scraper.get(url, timeout=timeout)
            r.raise_for_status()
            return r.json()
        else:
            raise RuntimeError("cloudscraper not available")
    except Exception as e:
        print(f"[API] cloudscraper failed: {e}, trying requests...")
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()


def get_series_info(series_id: str) -> dict:
    key = f"series_info_{series_id}"
    cached = cache_get(key, SERIES_INFO_TTL)
    if cached:
        return cached
    _load_cached_data()
    sid = str(series_id)
    if sid in _cached_episodes:
        ep_data = _cached_episodes[sid]
        episodes = {}
        for season, eps in ep_data.get("episodes", {}).items():
            episodes[season] = []
            for ep in eps:
                episodes[season].append({
                    "episode_num": ep.get("e"),
                    "stream_id": ep.get("s"),
                    "container_extension": ep.get("x", "mp4"),
                    "title": ep.get("t", ""),
                })
        result = {"episodes": episodes}
        cache_set(key, result)
        return result
    if not USE_CACHED:
        try:
            data = api_call("get_series_info", f"&series_id={series_id}", timeout=120)
            cache_set(key, data)
            return data
        except Exception as e:
            print(f"[API] Failed to get series info {series_id}: {e}")
    return {}


def get_all_vod() -> list[dict]:
    cached = cache_get("all_vod")
    if cached:
        return cached
    data = _load_local_json("vod_list.json") or _load_github_json("vod_list.json")
    if data and isinstance(data, list):
        cache_set("all_vod", data)
        return data
    if not USE_CACHED:
        try:
            data = api_call("get_vod_streams")
            cache_set("all_vod", data)
            return data
        except Exception as e:
            print(f"[API] Failed to load VOD: {e}")
    return []


# ─── Stremio Manifest ───
MANIFEST = {
    "id": "com.anime3rb.stream",
    "version": "2.0.0",
    "name": "Anime3rb بث",
    "description": "روابط بث الأنمي من anime3rb.vip — يعمل مع أي كتالوج يستخدم Kitsu IDs",
    "logo": "https://anime3rb.vip/favicon.ico",
    "resources": [
        {
            "name": "stream",
            "types": ["series", "movie"],
            "idPrefixes": ["kitsu:"],
        },
    ],
    "types": ["series", "movie"],
    "catalogs": [],
    "behaviorHints": {"configurable": False},
}


def stremio_response(data: dict) -> Response:
    return Response(
        content=json.dumps(data, ensure_ascii=False),
        media_type="application/json",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "*",
            "Cache-Control": "max-age=3600, public",
        },
    )


# ─── FastAPI App ───
app = FastAPI(title="Anime3rb Stream Addon")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/manifest.json")
def manifest():
    return stremio_response(MANIFEST)


@app.get("/stream/{content_type}/{stream_id}.json")
def stream(content_type: str, stream_id: str):
    """Handle stream requests using Kitsu IDs.
    
    Format: kitsu:{kitsu_id}:{season}:{episode}
    Example: kitsu:210:1:1202 (Detective Conan, Season 1, Episode 1202)
    """
    _load_cached_data()
    print(f"[Stream] REQUEST: content_type={content_type} stream_id={stream_id}")
    try:
        if not stream_id.startswith("kitsu:"):
            print(f"[Stream] REJECTED: does not start with 'kitsu:' -> '{stream_id[:30]}'")
            return stremio_response({"streams": []})

        parts = stream_id.split(":")
        # kitsu:ID:season:episode or kitsu:ID (movie)
        kitsu_id = parts[1] if len(parts) > 1 else ""

        if not kitsu_id:
            return stremio_response({"streams": []})

        # Look up anime3rb series_id from Kitsu map
        series_id = _kitsu_map.get(kitsu_id)
        if not series_id:
            print(f"[Stream] Kitsu ID {kitsu_id} not found in map")
            return stremio_response({"streams": []})

        if content_type == "series":
            if len(parts) < 4:
                return stremio_response({"streams": []})

            season_num = parts[2]
            episode_num = parts[3]

            data = get_series_info(series_id)
            if not data or "episodes" not in data:
                return stremio_response({"streams": []})

            season_eps = data["episodes"].get(season_num)
            if not season_eps:
                # Try season "1" as fallback (most anime3rb entries have 1 season)
                season_eps = data["episodes"].get("1")
            if not season_eps:
                return stremio_response({"streams": []})

            ep = next(
                (e for e in season_eps if str(e.get("episode_num")) == episode_num),
                None,
            )
            if not ep:
                return stremio_response({"streams": []})

            ext = ep.get("container_extension", "mp4")
            stream_url = f"{BASE_URL}/series/{USERNAME}/{PASSWORD}/{ep['stream_id']}.{ext}"

            return stremio_response({
                "streams": [{
                    "url": stream_url,
                    "title": f"Anime3rb\n{ep.get('title', 'Episode ' + episode_num)}",
                    "name": "Anime3rb",
                    "behaviorHints": {"notWebReady": True},
                }]
            })

        if content_type == "movie":
            # For movies, series_id is actually the vod stream_id
            vod_id = series_id
            all_vod = get_all_vod()
            vod_item = next(
                (v for v in all_vod if str(v.get("stream_id")) == vod_id), None
            ) if isinstance(all_vod, list) else None
            ext = (vod_item.get("container_extension") if vod_item else None) or "mp4"
            stream_url = f"{BASE_URL}/movie/{USERNAME}/{PASSWORD}/{vod_id}.{ext}"

            return stremio_response({
                "streams": [{
                    "url": stream_url,
                    "title": f"Anime3rb\n{vod_item.get('name', '') if vod_item else ''}",
                    "name": "Anime3rb",
                    "behaviorHints": {"notWebReady": True},
                }]
            })

    except Exception as e:
        print(f"[Stream] Error: {e}")

    return stremio_response({"streams": []})


@app.get("/debug/{content_type}/{stream_id}")
def debug_stream(content_type: str, stream_id: str):
    """Debug endpoint to check what happens with a stream request."""
    _load_cached_data()
    result = {"raw_stream_id": stream_id, "content_type": content_type}

    parts = stream_id.split(":")
    result["parts"] = parts
    result["starts_with_kitsu"] = stream_id.startswith("kitsu:")

    if len(parts) > 1:
        kitsu_id = parts[1]
        result["kitsu_id"] = kitsu_id
        result["found_in_map"] = kitsu_id in _kitsu_map
        if kitsu_id in _kitsu_map:
            result["anime3rb_series_id"] = _kitsu_map[kitsu_id]
    
    if len(parts) >= 4:
        result["season"] = parts[2]
        result["episode"] = parts[3]

    result["kitsu_map_size"] = len(_kitsu_map)
    result["sample_keys"] = list(_kitsu_map.keys())[:10]
    return result


@app.get("/health")
def health():
    _load_cached_data()
    return {
        "status": "ok",
        "kitsu_map_size": len(_kitsu_map),
        "cached_episodes": len(_cached_episodes),
        "cache_size_mb": round(_cache_size_mb(), 1),
        "use_cached": USE_CACHED,
    }


@app.on_event("startup")
def startup():
    _load_cached_data()
    print(f"[Addon] Stream-only addon ready!")
    print(f"[Addon] Kitsu map: {len(_kitsu_map)} entries")
    print(f"[Addon] Episodes: {len(_cached_episodes)} series")
    print(f"[Addon] Install: http://localhost:8000/manifest.json")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
