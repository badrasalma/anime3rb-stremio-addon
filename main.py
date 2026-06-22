"""Anime3rb Stremio IPTV Addon.

Fetches everything directly from the anime3rb Xtream API as-is.
Categories, series, episodes, movies — all from the source.
"""
import json
import os
import sys
import time
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote
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

# ─── Cloudscraper ───
def _make_scraper():
    if cloudscraper:
        return cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "linux"}
        )
    return None

scraper = _make_scraper()

# ─── In-memory cache ───
CACHE_TTL = 6 * 60 * 60
SERIES_INFO_TTL = 1 * 60 * 60
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
    protected = {"all_series", "all_vod", "series_categories", "vod_categories"}
    while _cache_size_mb() > MAX_CACHE_MB and len(_cache) > len(protected):
        for key in list(_cache.keys()):
            if key not in protected:
                _cache.pop(key, None)
                _cache_ts.pop(key, None)
                break
        else:
            break


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


# ─── Xtream API ───
def api_call(action: str = "", extra: str = "", timeout: int = 60) -> Any:
    global scraper
    url = f"{BASE_URL}/player_api.php?username={USERNAME}&password={PASSWORD}"
    if action:
        url += f"&action={action}"
    if extra:
        url += extra
    for attempt in range(3):
        try:
            if scraper:
                r = scraper.get(url, timeout=timeout)
                r.raise_for_status()
                return r.json()
            else:
                raise RuntimeError("cloudscraper not available")
        except Exception as e:
            print(f"[API] Attempt {attempt+1} failed: {e}")
            if attempt < 2:
                time.sleep(2)
                scraper = _make_scraper()
    # Final fallback
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ─── Data fetchers (all from server) ───
def get_series_categories() -> list[dict]:
    cached = cache_get("series_categories")
    if cached is not None:
        return cached
    try:
        data = api_call("get_series_categories")
        if isinstance(data, list):
            cache_set("series_categories", data)
            return data
    except Exception as e:
        print(f"[API] get_series_categories failed: {e}")
    return []


def get_vod_categories() -> list[dict]:
    cached = cache_get("vod_categories")
    if cached is not None:
        return cached
    try:
        data = api_call("get_vod_categories")
        if isinstance(data, list):
            cache_set("vod_categories", data)
            return data
    except Exception as e:
        print(f"[API] get_vod_categories failed: {e}")
    return []


def get_all_series() -> list[dict]:
    cached = cache_get("all_series")
    if cached is not None:
        return cached
    try:
        data = api_call("get_series", timeout=90)
        if isinstance(data, list):
            cache_set("all_series", data)
            return data
    except Exception as e:
        print(f"[API] get_series failed: {e}")
    return []


def get_all_vod() -> list[dict]:
    cached = cache_get("all_vod")
    if cached is not None:
        return cached
    try:
        data = api_call("get_vod_streams", timeout=90)
        if isinstance(data, list):
            cache_set("all_vod", data)
            return data
    except Exception as e:
        print(f"[API] get_vod_streams failed: {e}")
    return []


def get_series_info(series_id: str) -> dict:
    key = f"series_info_{series_id}"
    cached = cache_get(key, SERIES_INFO_TTL)
    if cached is not None:
        return cached
    try:
        data = api_call("get_series_info", f"&series_id={series_id}", timeout=120)
        if data:
            cache_set(key, data)
            return data
    except Exception as e:
        print(f"[API] get_series_info {series_id} failed: {e}")
    return {}


def get_vod_info(vod_id: str) -> dict:
    key = f"vod_info_{vod_id}"
    cached = cache_get(key, SERIES_INFO_TTL)
    if cached is not None:
        return cached
    try:
        data = api_call("get_vod_info", f"&vod_id={vod_id}", timeout=60)
        if data:
            cache_set(key, data)
            return data
    except Exception as e:
        print(f"[API] get_vod_info {vod_id} failed: {e}")
    return {}


# ─── Build manifest from server categories ───
def _build_manifest(series_cats: list[dict], vod_cats: list[dict]) -> dict:
    catalogs = []

    # One catalog per series category (from server)
    for cat in series_cats:
        cid = cat.get("category_id", "")
        cname = cat.get("category_name", "")
        catalogs.append({
            "type": "series",
            "id": f"anime3rb_series_{cid}",
            "name": cname,
            "extra": [
                {"name": "search", "isRequired": False},
                {"name": "skip", "isRequired": False},
            ],
        })

    # One catalog per VOD category (from server)
    for cat in vod_cats:
        cid = cat.get("category_id", "")
        cname = cat.get("category_name", "")
        catalogs.append({
            "type": "movie",
            "id": f"anime3rb_vod_{cid}",
            "name": cname,
            "extra": [
                {"name": "search", "isRequired": False},
                {"name": "skip", "isRequired": False},
            ],
        })

    return {
        "id": "com.anime3rb.iptv",
        "version": "5.0.0",
        "name": "Anime3rb أنمي",
        "description": "أنمي عرب — IPTV",
        "logo": "https://anime3rb.vip/favicon.ico",
        "resources": [
            "catalog",
            {
                "name": "meta",
                "types": ["series", "movie"],
                "idPrefixes": ["anime3rb_"],
            },
            {
                "name": "stream",
                "types": ["series", "movie"],
                "idPrefixes": ["anime3rb_"],
            },
        ],
        "types": ["series", "movie"],
        "catalogs": catalogs,
        "behaviorHints": {"configurable": False},
    }


# ─── FastAPI ───
app = FastAPI(title="Anime3rb IPTV Addon")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Manifest is built dynamically from server categories
_manifest: dict | None = None
_manifest_ts: float = 0


def _get_manifest() -> dict:
    global _manifest, _manifest_ts
    if _manifest and time.time() - _manifest_ts < CACHE_TTL:
        return _manifest
    series_cats = get_series_categories()
    vod_cats = get_vod_categories()
    _manifest = _build_manifest(series_cats, vod_cats)
    _manifest_ts = time.time()
    return _manifest


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


def _safe_int(val, default: int = 0) -> int:
    if isinstance(val, int):
        return val
    s = str(val).strip()
    if not s:
        return default
    import re
    m = re.match(r'(\d+)', s)
    return int(m.group(1)) if m else default


# ─── Routes ───
@app.get("/manifest.json")
def manifest():
    return stremio_response(_get_manifest())


@app.get("/catalog/{content_type}/{catalog_id}.json")
@app.get("/catalog/{content_type}/{catalog_id}/{extra_params}.json")
def catalog(content_type: str, catalog_id: str, extra_params: str = ""):
    extras = {}
    if extra_params:
        for part in extra_params.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                extras[k] = v
    skip = int(extras.get("skip", 0))
    search_q = unquote(extras["search"]).lower().strip() if extras.get("search") else ""

    try:
        # ── Series catalog ──
        if content_type == "series" and catalog_id.startswith("anime3rb_series_"):
            cat_id = catalog_id.replace("anime3rb_series_", "")
            all_series = get_all_series()
            # Filter by category
            series = [s for s in all_series if str(s.get("category_id")) == cat_id]
            # Search
            if search_q:
                series = [
                    s for s in series
                    if search_q in (s.get("name") or "").lower()
                    or search_q in (s.get("plot") or "").lower()
                ]
            # Paginate
            page = series[skip : skip + 100]
            metas = []
            for s in page:
                sid = str(s.get("series_id", ""))
                meta: dict[str, Any] = {
                    "id": f"anime3rb_s_{sid}",
                    "type": "series",
                    "name": s.get("name", ""),
                    "posterShape": "poster",
                }
                if s.get("cover"):
                    meta["poster"] = s["cover"]
                if s.get("plot"):
                    meta["description"] = s["plot"]
                if s.get("genre"):
                    meta["genres"] = [g.strip() for g in s["genre"].split(",") if g.strip()]
                if s.get("releaseDate"):
                    meta["releaseInfo"] = str(s["releaseDate"])[:4]
                if s.get("rating"):
                    meta["imdbRating"] = str(s["rating"])
                metas.append(meta)
            return stremio_response({"metas": metas})

        # ── VOD catalog ──
        if content_type == "movie" and catalog_id.startswith("anime3rb_vod_"):
            cat_id = catalog_id.replace("anime3rb_vod_", "")
            all_vod = get_all_vod()
            vod = [v for v in all_vod if str(v.get("category_id")) == cat_id]
            if search_q:
                vod = [
                    v for v in vod
                    if search_q in (v.get("name") or "").lower()
                ]
            page = vod[skip : skip + 100]
            metas = []
            for v in page:
                vid = str(v.get("stream_id", ""))
                meta: dict[str, Any] = {
                    "id": f"anime3rb_v_{vid}",
                    "type": "movie",
                    "name": v.get("name", ""),
                    "posterShape": "poster",
                }
                if v.get("stream_icon"):
                    meta["poster"] = v["stream_icon"]
                if v.get("rating"):
                    meta["imdbRating"] = str(v["rating"])
                metas.append(meta)
            return stremio_response({"metas": metas})

    except Exception as e:
        print(f"[Catalog] Error: {e}")
    return stremio_response({"metas": []})


@app.get("/meta/{content_type}/{meta_id}.json")
def meta(content_type: str, meta_id: str):
    try:
        # ── Series meta ──
        if content_type == "series" and meta_id.startswith("anime3rb_s_"):
            series_id = meta_id.replace("anime3rb_s_", "")
            data = get_series_info(series_id)
            if not data:
                return stremio_response({"meta": None})

            info = data.get("info", {})
            result: dict[str, Any] = {
                "id": meta_id,
                "type": "series",
                "name": info.get("name", ""),
                "posterShape": "poster",
            }
            if info.get("cover"):
                result["poster"] = info["cover"]
                result["background"] = info["cover"]
            if info.get("plot"):
                result["description"] = info["plot"]
            if info.get("genre"):
                result["genres"] = [g.strip() for g in info["genre"].split(",") if g.strip()]
            if info.get("releaseDate"):
                result["releaseInfo"] = str(info["releaseDate"])[:4]
            if info.get("rating"):
                result["imdbRating"] = str(info["rating"])
            if info.get("youtube_trailer"):
                result["trailers"] = [
                    {"source": info["youtube_trailer"], "type": "Trailer"}
                ]

            # Episodes — as the server provides them
            videos = []
            if "episodes" in data:
                for season_num, episodes in data["episodes"].items():
                    for ep in episodes:
                        vid: dict[str, Any] = {
                            "id": f"anime3rb_s_{series_id}:{season_num}:{ep.get('episode_num', '')}",
                            "title": ep.get("title", ""),
                            "season": _safe_int(season_num),
                            "episode": _safe_int(ep.get("episode_num", "")),
                        }
                        if info.get("cover"):
                            vid["thumbnail"] = info["cover"]
                        if ep.get("added"):
                            try:
                                vid["released"] = datetime.fromtimestamp(
                                    int(ep["added"]), tz=timezone.utc
                                ).isoformat()
                            except (ValueError, OSError):
                                pass
                        if ep.get("info", {}).get("plot"):
                            vid["overview"] = ep["info"]["plot"]
                        videos.append(vid)

            result["videos"] = videos
            return stremio_response({"meta": result})

        # ── Movie meta ──
        if content_type == "movie" and meta_id.startswith("anime3rb_v_"):
            vod_id = meta_id.replace("anime3rb_v_", "")
            vod_data = get_vod_info(vod_id)

            # Also check the VOD list for extra info
            all_vod = get_all_vod()
            vod_item = next(
                (v for v in all_vod if str(v.get("stream_id")) == vod_id), None
            ) if isinstance(all_vod, list) else None

            info = vod_data.get("info") if vod_data and "info" in vod_data else vod_item
            if not info:
                return stremio_response({"meta": None})

            result: dict[str, Any] = {
                "id": meta_id,
                "type": "movie",
                "name": info.get("name") or info.get("movie_name", ""),
                "posterShape": "poster",
            }
            poster = info.get("stream_icon") or info.get("cover") or info.get("movie_img")
            if poster:
                result["poster"] = poster
                result["background"] = poster
            if info.get("plot") or info.get("description"):
                result["description"] = info.get("plot") or info.get("description")
            if info.get("genre"):
                result["genres"] = [g.strip() for g in info["genre"].split(",") if g.strip()]
            if info.get("releaseDate"):
                result["releaseInfo"] = str(info["releaseDate"])[:4]
            if info.get("rating"):
                result["imdbRating"] = str(info["rating"])

            return stremio_response({"meta": result})

    except Exception as e:
        print(f"[Meta] Error: {e}")
    return stremio_response({"meta": None})


@app.get("/stream/{content_type}/{stream_id}.json")
def stream(content_type: str, stream_id: str):
    try:
        # ── Series stream ──
        if content_type == "series" and stream_id.startswith("anime3rb_s_"):
            raw = stream_id.replace("anime3rb_s_", "")
            parts = raw.split(":")
            if len(parts) < 3:
                return stremio_response({"streams": []})
            series_id, season_num, episode_num = parts[0], parts[1], parts[2]

            data = get_series_info(series_id)
            if not data or "episodes" not in data:
                return stremio_response({"streams": []})

            season_eps = data["episodes"].get(season_num)
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
                    "title": ep.get("title", f"Episode {episode_num}"),
                    "name": "Anime3rb",
                    "behaviorHints": {"notWebReady": True},
                }]
            })

        # ── Movie stream ──
        if content_type == "movie" and stream_id.startswith("anime3rb_v_"):
            vod_id = stream_id.replace("anime3rb_v_", "")
            all_vod = get_all_vod()
            vod_item = next(
                (v for v in all_vod if str(v.get("stream_id")) == vod_id), None
            ) if isinstance(all_vod, list) else None
            ext = (vod_item.get("container_extension") if vod_item else None) or "mp4"
            stream_url = f"{BASE_URL}/movie/{USERNAME}/{PASSWORD}/{vod_id}.{ext}"

            return stremio_response({
                "streams": [{
                    "url": stream_url,
                    "title": vod_item.get("name", "Anime3rb") if vod_item else "Anime3rb",
                    "name": "Anime3rb",
                    "behaviorHints": {"notWebReady": True},
                }]
            })

    except Exception as e:
        print(f"[Stream] Error: {e}")
    return stremio_response({"streams": []})


@app.get("/health")
def health():
    m = _get_manifest()
    return {
        "status": "ok",
        "version": m.get("version", "?"),
        "catalogs": len(m.get("catalogs", [])),
        "cache_size_mb": round(_cache_size_mb(), 1),
        "cache_keys": len(_cache),
    }


# ─── Background tasks ───
def _warm_cache() -> None:
    import gc
    try:
        time.sleep(2)
        print("[Startup] Loading categories...")
        get_series_categories()
        get_vod_categories()
        gc.collect()
        print("[Startup] Loading series list...")
        get_all_series()
        gc.collect()
        print("[Startup] Loading VOD list...")
        get_all_vod()
        gc.collect()
        print("[Startup] Cache warm-up complete!")
    except Exception as e:
        print(f"[Startup] Warm-up error: {e}")


def _background_refresh() -> None:
    while True:
        try:
            time.sleep(CACHE_TTL - 300)
            print("[Refresh] Refreshing catalog data...")
            with _lock:
                for key in ["series_categories", "vod_categories", "all_series", "all_vod"]:
                    _cache.pop(key, None)
                    _cache_ts.pop(key, None)
            get_series_categories()
            get_vod_categories()
            get_all_series()
            get_all_vod()
            global _manifest_ts
            _manifest_ts = 0
            print("[Refresh] Done!")
        except Exception as e:
            print(f"[Refresh] Error: {e}")


@app.on_event("startup")
def startup():
    threading.Thread(target=_warm_cache, daemon=True).start()
    threading.Thread(target=_background_refresh, daemon=True).start()
    print("[Addon] Anime3rb IPTV v5.0.0 ready!")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
