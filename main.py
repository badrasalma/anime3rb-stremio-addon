"""Anime3rb Stremio Addon — Full IPTV with catalogs, meta, streams + TVDB artwork.

Fetches everything LIVE from anime3rb.vip Xtream API.
TVDB provides backgrounds and logos for all anime.
"""
import json
import os
import re
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
TVDB_API_KEY = os.environ.get("TVDB_API_KEY", "962fd58f-6940-4666-8d0c-8d918815ffba")

_tvdb_token: str = ""
_tvdb_token_ts: float = 0

# ─── Cloudscraper session ───
scraper = (
    cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "linux"})
    if cloudscraper else None
)

# ─── Cache ───
CACHE_TTL = 6 * 60 * 60        # 6 hours for catalog lists
SERIES_INFO_TTL = 12 * 60 * 60  # 12 hours for episode data
TVDB_TTL = 24 * 60 * 60         # 24 hours for TVDB lookups
MAX_CACHE_MB = 60
PROTECTED_KEYS = {"series_categories", "all_series", "all_vod"}

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
    while _cache_size_mb() > MAX_CACHE_MB and len(_cache) > len(PROTECTED_KEYS):
        for key in list(_cache.keys()):
            if key not in PROTECTED_KEYS:
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


# ─── GitHub data fallback (for when API is blocked on Render) ───
GITHUB_DATA_URL = os.environ.get(
    "GITHUB_DATA_URL",
    "https://raw.githubusercontent.com/badrasalma/anime3rb-stremio-addon/devin/deploy/data"
)

def _load_github_json(filename: str) -> Any:
    url = f"{GITHUB_DATA_URL}/{filename}"
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[GitHub] Failed to load {filename}: {e}")
        return None


# ─── Xtream API ───
def _make_scraper():
    if cloudscraper:
        return cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "linux"})
    return None


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
                time.sleep(3)
                scraper = _make_scraper()  # Re-init scraper
    # Final fallback: plain requests
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[API] All attempts failed: {e}")
        raise


def get_series_categories() -> list[dict]:
    cached = cache_get("series_categories")
    if cached:
        return cached
    data = api_call("get_series_categories")
    if isinstance(data, list):
        cache_set("series_categories", data)
    return data if isinstance(data, list) else []


def get_all_series() -> list[dict]:
    cached = cache_get("all_series")
    if cached:
        return cached
    try:
        data = api_call("get_series", timeout=90)
        if isinstance(data, list):
            cache_set("all_series", data)
            return data
    except Exception as e:
        print(f"[API] get_series failed, trying GitHub fallback: {e}")
    # GitHub fallback
    data = _load_github_json("series_list.json")
    if isinstance(data, list):
        cache_set("all_series", data)
        return data
    return []


def get_series_info(series_id: str) -> dict:
    key = f"series_info_{series_id}"
    cached = cache_get(key, SERIES_INFO_TTL)
    if cached:
        return cached
    try:
        data = api_call("get_series_info", f"&series_id={series_id}", timeout=120)
        if data:
            cache_set(key, data)
        return data or {}
    except Exception as e:
        print(f"[API] Failed to get series info {series_id}: {e}")
    return {}


def get_all_vod() -> list[dict]:
    cached = cache_get("all_vod")
    if cached:
        return cached
    try:
        data = api_call("get_vod_streams", timeout=90)
        if isinstance(data, list):
            cache_set("all_vod", data)
            return data
    except Exception as e:
        print(f"[API] get_vod_streams failed: {e}")
    return []


def get_vod_info(vod_id: str) -> dict:
    key = f"vod_info_{vod_id}"
    cached = cache_get(key, SERIES_INFO_TTL)
    if cached:
        return cached
    try:
        data = api_call("get_vod_info", f"&vod_id={vod_id}", timeout=60)
        if data:
            cache_set(key, data)
        return data or {}
    except Exception as e:
        print(f"[API] Failed to get VOD info {vod_id}: {e}")
    return {}


# ─── TVDB API ───
def _tvdb_login() -> str:
    global _tvdb_token, _tvdb_token_ts
    if _tvdb_token and (time.time() - _tvdb_token_ts) < 25 * 24 * 3600:
        return _tvdb_token
    try:
        r = requests.post(
            "https://api4.thetvdb.com/v4/login",
            json={"apikey": TVDB_API_KEY},
            timeout=10,
        )
        r.raise_for_status()
        _tvdb_token = r.json().get("data", {}).get("token", "")
        _tvdb_token_ts = time.time()
        print("[TVDB] Logged in successfully")
        return _tvdb_token
    except Exception as e:
        print(f"[TVDB] Login error: {e}")
        return ""


def _tvdb_get(path: str) -> dict | None:
    token = _tvdb_login()
    if not token:
        return None
    try:
        r = requests.get(
            f"https://api4.thetvdb.com/v4/{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("data")
    except Exception:
        return None


def _normalize(text: str) -> str:
    return re.sub(r'[^\w\s]', ' ', text.lower()).strip()


def _tvdb_search(anime_name: str, content_type: str = "series") -> int | None:
    tvdb_type = "movie" if content_type == "movie" else "series"
    key = f"tvdb_id_{tvdb_type}_{_normalize(anime_name)}"
    cached = cache_get(key, TVDB_TTL)
    if cached is not None:
        return cached if cached != 0 else None
    try:
        encoded = requests.utils.quote(anime_name)
        data = _tvdb_get(f"search?query={encoded}&type={tvdb_type}")
        if not data and tvdb_type == "movie":
            data = _tvdb_get(f"search?query={encoded}")
        if data:
            norm_name = _normalize(anime_name)
            for r in data:
                if _normalize(r.get("name", "")) == norm_name:
                    tvdb_id = int(r["tvdb_id"])
                    cache_set(key, tvdb_id)
                    return tvdb_id
            for r in data:
                aliases = r.get("aliases", [])
                if isinstance(aliases, list):
                    for alias in aliases:
                        alias_name = alias if isinstance(alias, str) else alias.get("name", "")
                        if _normalize(alias_name) == norm_name:
                            tvdb_id = int(r["tvdb_id"])
                            cache_set(key, tvdb_id)
                            return tvdb_id
            tvdb_id = int(data[0]["tvdb_id"])
            cache_set(key, tvdb_id)
            return tvdb_id
        cache_set(key, 0)
        return None
    except Exception:
        cache_set(key, 0)
        return None


def _tvdb_artwork(tvdb_id: int, content_type: str = "series") -> dict:
    key = f"tvdb_art_{content_type}_{tvdb_id}"
    cached = cache_get(key, TVDB_TTL)
    if cached is not None:
        return cached
    result = {"poster": "", "background": "", "logo": ""}
    endpoint = "movies" if content_type == "movie" else "series"
    data = _tvdb_get(f"{endpoint}/{tvdb_id}/extended")
    if not data:
        cache_set(key, result)
        return result
    artworks = data.get("artworks", [])
    for a in artworks:
        url = a.get("image", "")
        if not url:
            continue
        art_type = a.get("type", 0)
        if art_type in (2, 14) and not result["poster"]:
            result["poster"] = url
        elif art_type in (3, 15) and not result["background"]:
            result["background"] = url
        elif art_type == 23 and not result["logo"]:
            result["logo"] = url
        if all(result.values()):
            break
    cache_set(key, result)
    return result


def _get_tvdb_art(name: str, content_type: str = "series") -> dict:
    """Get TVDB artwork for an anime by name. Returns {poster, background, logo}."""
    if not name:
        return {"poster": "", "background": "", "logo": ""}
    tvdb_id = _tvdb_search(name, content_type)
    if not tvdb_id and content_type == "movie":
        tvdb_id = _tvdb_search(name, "series")
        if tvdb_id:
            return _tvdb_artwork(tvdb_id, "series")
    if tvdb_id:
        return _tvdb_artwork(tvdb_id, content_type)
    return {"poster": "", "background": "", "logo": ""}


# ─── Search helpers ───
_SEARCH_ALIASES: dict[str, list[str]] = {
    "seven deadly sins": ["nanatsu no taizai"],
    "attack on titan": ["shingeki no kyojin"],
    "demon slayer": ["kimetsu no yaiba"],
    "my hero academia": ["boku no hero academia"],
    "hunter x hunter": ["hunter x hunter"],
    "one punch man": ["one punch man"],
    "sword art online": ["sword art online"],
    "black clover": ["black clover"],
    "tokyo ghoul": ["tokyo ghoul"],
    "death note": ["death note"],
    "fullmetal alchemist": ["fullmetal alchemist", "hagane no renkinjutsushi"],
    "detective conan": ["meitantei conan"],
    "case closed": ["meitantei conan"],
    "dragon ball": ["dragon ball"],
    "fairy tail": ["fairy tail"],
    "bleach": ["bleach"],
    "naruto": ["naruto"],
    "jujutsu kaisen": ["jujutsu kaisen"],
    "spy x family": ["spy x family"],
    "chainsaw man": ["chainsaw man"],
    "one piece": ["one piece"],
    "blue lock": ["blue lock"],
    "vinland saga": ["vinland saga"],
    "the rising of the shield hero": ["tate no yuusha no nariagari"],
    "shield hero": ["tate no yuusha no nariagari"],
    "re zero": ["re:zero", "rezero"],
    "konosuba": ["kono subarashii"],
    "classroom of the elite": ["youkoso jitsuryoku"],
    "that time i got reincarnated as a slime": ["tensei shitara slime datta ken"],
    "mushoku tensei": ["mushoku tensei"],
    "solo leveling": ["ore dake level up"],
    "mob psycho": ["mob psycho"],
    "tower of god": ["kami no tou"],
    "overlord": ["overlord"],
    "no game no life": ["no game no life"],
    "steins gate": ["steins;gate", "steins gate"],
    "الخطايا السبع": ["nanatsu no taizai"],
    "هجوم العمالقة": ["shingeki no kyojin"],
    "قاتل الشياطين": ["kimetsu no yaiba"],
    "بطلي الأكاديمي": ["boku no hero academia"],
    "القناص": ["hunter x hunter"],
    "ون بيس": ["one piece"],
    "ناروتو": ["naruto"],
    "بليتش": ["bleach"],
    "المحقق كونان": ["meitantei conan"],
    "كونان": ["meitantei conan"],
    "دراغون بول": ["dragon ball"],
    "مذكرة الموت": ["death note"],
    "طوكيو غول": ["tokyo ghoul"],
    "جوجوتسو كايسن": ["jujutsu kaisen"],
    "البرسيم الأسود": ["black clover"],
    "ذيل الجنية": ["fairy tail"],
    "سورد ارت": ["sword art online"],
    "بلو لوك": ["blue lock"],
    "فينلاند ساغا": ["vinland saga"],
    "موشوكو تنسي": ["mushoku tensei"],
    "صعود المستوى": ["ore dake level up"],
    "برج الإله": ["kami no tou"],
}


def _expand_search(query: str) -> list[str]:
    queries = [query]
    q_lower = query.lower().strip()
    for eng, aliases in _SEARCH_ALIASES.items():
        if eng in q_lower or q_lower in eng:
            queries.extend(aliases)
    return queries


def _search_score(query: str, name: str, plot: str) -> float:
    q = query.lower().strip()
    name_lower = name.lower()
    plot_lower = plot.lower() if plot else ""
    if q == name_lower:
        return 100.0
    if q in name_lower:
        return 90.0
    if name_lower.startswith(q):
        return 85.0
    words = q.split()
    if words and all(w in name_lower for w in words):
        return 80.0
    combined = name_lower + " " + plot_lower
    if words and all(w in combined for w in words):
        return 60.0
    if len(words) >= 2:
        hits = sum(1 for w in words if w in name_lower)
        ratio = hits / len(words)
        if ratio >= 0.5:
            return 40.0 * ratio
    if len(words) == 1 and len(q) >= 3 and q in name_lower:
        return 50.0
    return 0.0


def _safe_int(val, default: int = 0) -> int:
    if isinstance(val, int):
        return val
    s = str(val).strip()
    if not s:
        return default
    m = re.match(r'(\d+)', s)
    return int(m.group(1)) if m else default


# ─── Genre catalog mapping ───
_GENRE_CATALOGS = {
    "anime3rb_action": "أكشن",
    "anime3rb_comedy": "كوميدي",
    "anime3rb_fantasy": "خيال",
    "anime3rb_shounen": "شونين",
    "anime3rb_adventure": "مغامرة",
    "anime3rb_drama": "دراما",
    "anime3rb_scifi": "خيال علمي",
    "anime3rb_seinen": "سينين",
    "anime3rb_supernatural": "خارق للطبيعة",
    "anime3rb_mystery": "غموض",
    "anime3rb_isekai": "إيسيكاي",
    "anime3rb_mecha": "ميكا",
    "anime3rb_thriller": "تشويق",
}


# ─── Stremio Manifest ───
MANIFEST = {
    "id": "com.anime3rb.xtream",
    "version": "4.0.0",
    "name": "Anime3rb أنمي",
    "description": "مشاهدة الأنمي والأفلام من anime3rb.vip — كتالوجات + بث مباشر من المصدر + خلفيات ولوغو TVDB",
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
    "catalogs": [
        {
            "type": "series",
            "id": "anime3rb_series",
            "name": "مسلسلات الأنمي",
            "extra": [
                {"name": "search", "isRequired": False},
                {"name": "skip", "isRequired": False},
            ],
        },
        {
            "type": "series",
            "id": "anime3rb_new",
            "name": "أنمي - جديد",
            "extra": [{"name": "skip", "isRequired": False}],
        },
        {
            "type": "series",
            "id": "anime3rb_trending",
            "name": "أنمي - الشائع",
            "extra": [{"name": "skip", "isRequired": False}],
        },
        {
            "type": "series",
            "id": "anime3rb_top_rated",
            "name": "أنمي - Top Rated",
            "extra": [{"name": "skip", "isRequired": False}],
        },
        {
            "type": "series",
            "id": "anime3rb_action",
            "name": "أنمي - أكشن",
            "extra": [{"name": "skip", "isRequired": False}],
        },
        {
            "type": "series",
            "id": "anime3rb_comedy",
            "name": "أنمي - كوميدي",
            "extra": [{"name": "skip", "isRequired": False}],
        },
        {
            "type": "series",
            "id": "anime3rb_fantasy",
            "name": "أنمي - خيال",
            "extra": [{"name": "skip", "isRequired": False}],
        },
        {
            "type": "series",
            "id": "anime3rb_shounen",
            "name": "أنمي - شونين",
            "extra": [{"name": "skip", "isRequired": False}],
        },
        {
            "type": "series",
            "id": "anime3rb_adventure",
            "name": "أنمي - مغامرة",
            "extra": [{"name": "skip", "isRequired": False}],
        },
        {
            "type": "series",
            "id": "anime3rb_drama",
            "name": "أنمي - دراما",
            "extra": [{"name": "skip", "isRequired": False}],
        },
        {
            "type": "series",
            "id": "anime3rb_scifi",
            "name": "أنمي - خيال علمي",
            "extra": [{"name": "skip", "isRequired": False}],
        },
        {
            "type": "series",
            "id": "anime3rb_seinen",
            "name": "أنمي - سينين",
            "extra": [{"name": "skip", "isRequired": False}],
        },
        {
            "type": "series",
            "id": "anime3rb_supernatural",
            "name": "أنمي - خارق للطبيعة",
            "extra": [{"name": "skip", "isRequired": False}],
        },
        {
            "type": "series",
            "id": "anime3rb_mystery",
            "name": "أنمي - غموض",
            "extra": [{"name": "skip", "isRequired": False}],
        },
        {
            "type": "series",
            "id": "anime3rb_isekai",
            "name": "أنمي - إيسيكاي",
            "extra": [{"name": "skip", "isRequired": False}],
        },
        {
            "type": "series",
            "id": "anime3rb_mecha",
            "name": "أنمي - ميكا",
            "extra": [{"name": "skip", "isRequired": False}],
        },
        {
            "type": "series",
            "id": "anime3rb_thriller",
            "name": "أنمي - تشويق",
            "extra": [{"name": "skip", "isRequired": False}],
        },
        {
            "type": "movie",
            "id": "anime3rb_movies",
            "name": "أفلام الأنمي",
            "extra": [
                {"name": "search", "isRequired": False},
                {"name": "skip", "isRequired": False},
            ],
        },
    ],
    "behaviorHints": {"configurable": False},
}


# ─── FastAPI App ───
app = FastAPI(title="Anime3rb Stremio Addon")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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


# ─── Catalog helpers ───
def _build_series_metas(series_list: list, skip: int = 0) -> list:
    page = series_list[skip : skip + 100]
    metas = []
    for s in page:
        sid = str(s["series_id"])
        meta: dict[str, Any] = {
            "id": f"anime3rb_series_{sid}",
            "type": "series",
            "name": s.get("name", ""),
            "posterShape": "poster",
        }
        if s.get("cover"):
            meta["poster"] = s["cover"]
        if s.get("plot"):
            meta["description"] = s["plot"]
        if s.get("genre"):
            meta["genres"] = [g.strip() for g in s["genre"].split(",")]
        if s.get("releaseDate"):
            meta["releaseInfo"] = s["releaseDate"][:4]
        if s.get("rating"):
            meta["imdbRating"] = s["rating"]
        metas.append(meta)
    return metas


def _build_vod_metas(vod_list: list, skip: int = 0) -> list:
    page = vod_list[skip : skip + 100]
    metas = []
    for v in page:
        meta: dict[str, Any] = {
            "id": f"anime3rb_vod_{v.get('stream_id', '')}",
            "type": "movie",
            "name": v.get("name", ""),
            "posterShape": "poster",
        }
        if v.get("stream_icon") or v.get("cover"):
            meta["poster"] = v.get("stream_icon") or v.get("cover")
        if v.get("plot"):
            meta["description"] = v["plot"]
        if v.get("genre"):
            meta["genres"] = [g.strip() for g in v["genre"].split(",")]
        if v.get("releaseDate"):
            meta["releaseInfo"] = v["releaseDate"][:4]
        if v.get("rating"):
            meta["imdbRating"] = v["rating"]
        metas.append(meta)
    return metas


# ─── Routes ───
@app.get("/manifest.json")
def manifest():
    return stremio_response(MANIFEST)


@app.get("/catalog/{content_type}/{catalog_id}.json")
@app.get("/catalog/{content_type}/{catalog_id}/{extra_params}.json")
def catalog(content_type: str, catalog_id: str, extra_params: str = ""):
    extras = {}
    if extra_params:
        for part in extra_params.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                extras[k] = v

    try:
        # ── Series catalogs ──
        if content_type == "series":
            if catalog_id == "anime3rb_series":
                series = get_all_series()
                if not isinstance(series, list):
                    return stremio_response({"metas": []})
                if extras.get("search"):
                    raw_q = unquote(extras["search"])
                    queries = _expand_search(raw_q)
                    scored: dict[str, tuple[float, dict]] = {}
                    for q in queries:
                        for s in series:
                            sid = str(s.get("series_id", ""))
                            score = _search_score(q, s.get("name", ""), s.get("plot", ""))
                            if score > 0 and (sid not in scored or score > scored[sid][0]):
                                scored[sid] = (score, s)
                    ranked = sorted(scored.values(), key=lambda x: x[0], reverse=True)
                    series = [s for _, s in ranked]
                skip = int(extras.get("skip", 0))
                return stremio_response({"metas": _build_series_metas(series, skip)})

            if catalog_id == "anime3rb_new":
                series = get_all_series()
                if not isinstance(series, list):
                    return stremio_response({"metas": []})
                series = sorted(
                    [s for s in series if s.get("releaseDate")],
                    key=lambda s: s.get("releaseDate", ""),
                    reverse=True,
                )
                skip = int(extras.get("skip", 0))
                return stremio_response({"metas": _build_series_metas(series, skip)})

            if catalog_id == "anime3rb_trending":
                series = get_all_series()
                if not isinstance(series, list):
                    return stremio_response({"metas": []})
                series = [s for s in series if s.get("releaseDate") and s.get("rating")]
                series = sorted(
                    series,
                    key=lambda s: (s.get("releaseDate", ""), float(s.get("rating", 0) or 0)),
                    reverse=True,
                )
                skip = int(extras.get("skip", 0))
                return stremio_response({"metas": _build_series_metas(series, skip)})

            if catalog_id == "anime3rb_top_rated":
                series = get_all_series()
                if not isinstance(series, list):
                    return stremio_response({"metas": []})
                series = sorted(
                    [s for s in series if s.get("rating")],
                    key=lambda s: float(s.get("rating", 0) or 0),
                    reverse=True,
                )
                skip = int(extras.get("skip", 0))
                return stremio_response({"metas": _build_series_metas(series, skip)})

            if catalog_id in _GENRE_CATALOGS:
                genre_name = _GENRE_CATALOGS[catalog_id]
                series = get_all_series()
                if not isinstance(series, list):
                    return stremio_response({"metas": []})
                series = [
                    s for s in series
                    if genre_name in [g.strip() for g in (s.get("genre") or "").split(",")]
                ]
                series = sorted(
                    series,
                    key=lambda s: float(s.get("rating", 0) or 0),
                    reverse=True,
                )
                skip = int(extras.get("skip", 0))
                return stremio_response({"metas": _build_series_metas(series, skip)})

        # ── Movie catalog ──
        if content_type == "movie" and catalog_id == "anime3rb_movies":
            vod = get_all_vod()
            if not isinstance(vod, list):
                return stremio_response({"metas": []})
            if extras.get("search"):
                raw_q = unquote(extras["search"])
                queries = _expand_search(raw_q)
                vod_scored: dict[str, tuple[float, dict]] = {}
                for q in queries:
                    for v in vod:
                        vid = str(v.get("stream_id", ""))
                        score = _search_score(q, v.get("name", ""), v.get("plot", ""))
                        if score > 0 and (vid not in vod_scored or score > vod_scored[vid][0]):
                            vod_scored[vid] = (score, v)
                ranked = sorted(vod_scored.values(), key=lambda x: x[0], reverse=True)
                vod = [v for _, v in ranked]
            skip = int(extras.get("skip", 0))
            return stremio_response({"metas": _build_vod_metas(vod, skip)})

    except Exception as e:
        print(f"[Catalog] Error: {e}")

    return stremio_response({"metas": []})


@app.get("/meta/{content_type}/{meta_id}.json")
def meta(content_type: str, meta_id: str):
    try:
        # ── Series meta ──
        if content_type == "series" and meta_id.startswith("anime3rb_series_"):
            series_id = meta_id.replace("anime3rb_series_", "")
            data = get_series_info(series_id)
            if not data or "info" not in data:
                return stremio_response({"meta": None})

            info = data["info"]
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
                result["genres"] = [g.strip() for g in info["genre"].split(",")]
            if info.get("releaseDate"):
                result["releaseInfo"] = info["releaseDate"][:4]
            if info.get("rating"):
                result["imdbRating"] = info["rating"]
            if info.get("youtube_trailer"):
                result["trailers"] = [
                    {"source": info["youtube_trailer"], "type": "Trailer"}
                ]

            # TVDB artwork: background + logo for every anime
            anime_name = info.get("name", "")
            tvdb_art = _get_tvdb_art(anime_name, "series")
            if tvdb_art.get("background"):
                result["background"] = tvdb_art["background"]
            if tvdb_art.get("logo"):
                result["logo"] = tvdb_art["logo"]
            if tvdb_art.get("poster"):
                result["poster"] = tvdb_art["poster"]

            # Build episode list (LIVE from API — always up to date)
            videos = []
            cover = info.get("cover", "")
            if "episodes" in data:
                for season_num, episodes in data["episodes"].items():
                    for ep in episodes:
                        ep_num_raw = ep.get("episode_num", "")
                        vid: dict[str, Any] = {
                            "id": f"anime3rb_series_{series_id}:{season_num}:{ep_num_raw}",
                            "title": ep.get("title", f"الحلقة {ep_num_raw}"),
                            "season": _safe_int(season_num),
                            "episode": _safe_int(ep_num_raw),
                        }
                        if cover:
                            vid["thumbnail"] = cover
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
        if content_type == "movie" and meta_id.startswith("anime3rb_vod_"):
            vod_id = meta_id.replace("anime3rb_vod_", "")
            vod_data = None
            try:
                vod_data = get_vod_info(vod_id)
            except Exception:
                pass

            all_vod = get_all_vod()
            vod_item = next(
                (v for v in all_vod if str(v.get("stream_id")) == vod_id), None
            ) if isinstance(all_vod, list) else None

            info = (
                vod_data.get("info") if vod_data and "info" in vod_data else vod_item
            )
            if not info:
                return stremio_response({"meta": None})

            movie_name = info.get("name") or info.get("movie_name", "")
            result: dict[str, Any] = {
                "id": meta_id,
                "type": "movie",
                "name": movie_name,
                "posterShape": "poster",
            }
            poster = (
                info.get("stream_icon")
                or info.get("cover")
                or info.get("movie_img")
            )
            if poster:
                result["poster"] = poster
                result["background"] = poster
            if info.get("plot") or info.get("description"):
                result["description"] = info.get("plot") or info.get("description")
            if info.get("genre"):
                result["genres"] = [g.strip() for g in info["genre"].split(",")]
            if info.get("releaseDate"):
                result["releaseInfo"] = info["releaseDate"][:4]
            if info.get("rating"):
                result["imdbRating"] = info["rating"]

            # TVDB artwork: background + logo for every movie
            tvdb_art = _get_tvdb_art(movie_name, "movie")
            if tvdb_art.get("background"):
                result["background"] = tvdb_art["background"]
            if tvdb_art.get("logo"):
                result["logo"] = tvdb_art["logo"]
            if tvdb_art.get("poster"):
                result["poster"] = tvdb_art["poster"]

            return stremio_response({"meta": result})

    except Exception as e:
        print(f"[Meta] Error: {e}")

    return stremio_response({"meta": None})


@app.get("/stream/{content_type}/{stream_id}.json")
def stream(content_type: str, stream_id: str):
    try:
        # ── Series stream ──
        if content_type == "series" and stream_id.startswith("anime3rb_series_"):
            raw = stream_id.replace("anime3rb_series_", "")
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
                    "title": f"{ep.get('title', 'الحلقة ' + episode_num)}",
                    "name": "Anime3rb",
                    "behaviorHints": {"notWebReady": True},
                }]
            })

        # ── Movie stream ──
        if content_type == "movie" and stream_id.startswith("anime3rb_vod_"):
            vod_id = stream_id.replace("anime3rb_vod_", "")
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
    return {
        "status": "ok",
        "version": MANIFEST["version"],
        "cache_size_mb": round(_cache_size_mb(), 1),
        "cache_keys": len(_cache),
    }


# ─── Background tasks ───
def _warm_cache() -> None:
    import gc
    try:
        time.sleep(2)
        print("[Cache] Warming: series list...")
        get_all_series()
        gc.collect()
        print("[Cache] Warming: VOD list...")
        get_all_vod()
        gc.collect()
        print("[Cache] Warming: categories...")
        get_series_categories()
        gc.collect()
        print("[Cache] Warm-up complete!")
    except Exception as e:
        print(f"[Cache] Warm-up error: {e}")


def _background_refresh() -> None:
    while True:
        try:
            time.sleep(CACHE_TTL - 300)
            print("[Cache] Background refresh...")
            with _lock:
                for key in ["series_categories", "all_series", "all_vod"]:
                    _cache.pop(key, None)
                    _cache_ts.pop(key, None)
            get_series_categories()
            get_all_series()
            get_all_vod()
            print("[Cache] Background refresh complete!")
        except Exception as e:
            print(f"[Cache] Background refresh error: {e}")


@app.on_event("startup")
def startup():
    t = threading.Thread(target=_warm_cache, daemon=True)
    t.start()
    r = threading.Thread(target=_background_refresh, daemon=True)
    r.start()
    print(f"[Addon] v{MANIFEST['version']} ready!")
    print(f"[Addon] Install: http://localhost:8000/manifest.json")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
