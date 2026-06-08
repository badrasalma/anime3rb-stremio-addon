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

# GitHub raw URL for cached data files
GITHUB_DATA_URL = os.environ.get(
    "GITHUB_DATA_URL",
    "https://raw.githubusercontent.com/badrasalma/anime3rb-stremio-addon/devin/deploy/data"
)
# Set USE_CACHED=1 to use pre-cached data files instead of live API
USE_CACHED = os.environ.get("USE_CACHED", "0") == "1"

# TVDB API key for anime artwork (backgrounds, logos)
TVDB_API_KEY = os.environ.get("TVDB_API_KEY", "962fd58f-6940-4666-8d0c-8d918815ffba")
_tvdb_token: str = ""
_tvdb_token_ts: float = 0

# ─── Cloudscraper session ───
scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "linux"}) if cloudscraper else None

# ─── Cache ───
CACHE_TTL = 6 * 60 * 60  # 6 hours for lists (series/VOD catalogs)
SERIES_INFO_TTL = 12 * 60 * 60  # 12 hours for series info (episode data rarely changes)
MAX_CACHE_MB = 60  # Max cache size in MB (~60MB leaves ~190MB for Python + app)
PROTECTED_KEYS = {"series_categories", "all_series", "all_vod"}  # Never evict these

_cache: OrderedDict[str, Any] = OrderedDict()
_cache_ts: dict[str, float] = {}
_lock = threading.Lock()


def _cache_size_mb() -> float:
    """Estimate total cache size in MB."""
    total = 0
    for v in _cache.values():
        try:
            total += sys.getsizeof(json.dumps(v, ensure_ascii=False))
        except (TypeError, ValueError):
            total += sys.getsizeof(v)
    return total / (1024 * 1024)


def _evict_if_needed() -> None:
    """Remove oldest non-protected entries if cache exceeds limit."""
    while _cache_size_mb() > MAX_CACHE_MB and len(_cache) > len(PROTECTED_KEYS):
        for key in list(_cache.keys()):
            if key not in PROTECTED_KEYS:
                _cache.pop(key, None)
                _cache_ts.pop(key, None)
                print(f"[Cache] Evicted: {key}")
                break
        else:
            break


def cache_get(key: str, ttl: int | None = None) -> Any | None:
    with _lock:
        t = ttl if ttl is not None else CACHE_TTL
        if key in _cache and time.time() - _cache_ts.get(key, 0) < t:
            _cache.move_to_end(key)  # Mark as recently used
            return _cache[key]
    return None


def cache_set(key: str, val: Any) -> None:
    with _lock:
        _cache[key] = val
        _cache_ts[key] = time.time()
        _cache.move_to_end(key)
        _evict_if_needed()


# ─── Pre-cached data store ───
_cached_episodes: dict = {}  # series_id -> episode data (loaded from GitHub)
_cached_data_ts: float = 0  # timestamp of last data reload
CACHED_DATA_TTL = 6 * 60 * 60  # Reload cached data every 6 hours
_english_names: dict[str, str] = {}  # series_id -> English display name


def _load_github_json(filename: str) -> Any:
    """Download a JSON file from the GitHub data directory."""
    url = f"{GITHUB_DATA_URL}/{filename}"
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[GitHub] Failed to load {filename}: {e}")
        return None


def _load_local_json(filename: str) -> Any:
    """Load a JSON file from local data directory."""
    data_dir = Path(__file__).parent / "data"
    fpath = data_dir / filename
    if fpath.exists():
        with open(fpath) as f:
            return json.load(f)
    return None


def _load_cached_data() -> None:
    """Load pre-cached data from GitHub or local files."""
    global _cached_episodes, _cached_data_ts, _english_names
    if time.time() - _cached_data_ts < CACHED_DATA_TTL and _cached_episodes:
        return  # Already loaded and fresh

    print("[Cache] Loading pre-cached data...")
    # Try local first, then GitHub
    for loader_name, loader in [("local", _load_local_json), ("GitHub", _load_github_json)]:
        eps = loader("episodes.json")
        if eps and isinstance(eps, dict) and len(eps) > 0:
            _cached_episodes = eps
            _cached_data_ts = time.time()
            print(f"[Cache] Loaded {len(eps)} series episode data from {loader_name}")
            break
    else:
        print("[Cache] WARNING: No pre-cached episode data available")

    # Load English names map
    for loader_name, loader in [("local", _load_local_json), ("GitHub", _load_github_json)]:
        names = loader("english_names.json")
        if names and isinstance(names, dict):
            _english_names = names
            print(f"[Cache] Loaded {len(names)} English name mappings from {loader_name}")
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


def get_series_categories() -> list[dict]:
    cached = cache_get("series_categories")
    if cached:
        return cached
    # Try pre-cached files
    data = _load_local_json("categories.json") or _load_github_json("categories.json")
    if data:
        cache_set("series_categories", data)
        return data
    if not USE_CACHED:
        data = api_call("get_series_categories")
        cache_set("series_categories", data)
        return data
    return []


_SERIES_FIELDS = {"name", "series_id", "cover", "plot", "genre", "category_id", "rating", "releaseDate"}
_VOD_FIELDS = {"name", "stream_id", "stream_type", "container_extension", "cover", "plot", "genre", "category_id", "rating", "releaseDate"}


def _trim(items: list[dict], fields: set[str]) -> list[dict]:
    result = []
    for item in items:
        d = {k: v for k, v in item.items() if k in fields}
        if "plot" in d and d["plot"] and len(d["plot"]) > 500:
            d["plot"] = d["plot"][:500] + "..."
        result.append(d)
    return result


def get_all_series() -> list[dict]:
    cached = cache_get("all_series")
    if cached:
        return cached
    # Try pre-cached files
    data = _load_local_json("series_list.json") or _load_github_json("series_list.json")
    if data and isinstance(data, list):
        print(f"[Cache] Loaded {len(data)} series from cached files")
        cache_set("all_series", data)
        return data
    if not USE_CACHED:
        try:
            raw = api_call("get_series")
            if not isinstance(raw, list):
                print(f"[API] get_series returned {type(raw).__name__}, expected list")
                return []
            data = _trim(raw, _SERIES_FIELDS)
            print(f"[API] Loaded {len(data)} series")
            cache_set("all_series", data)
            return data
        except Exception as e:
            print(f"[API] Failed to load series: {e}")
    return []


def get_series_info(series_id: str) -> dict:
    key = f"series_info_{series_id}"
    cached = cache_get(key, SERIES_INFO_TTL)
    if cached:
        return cached
    # Check pre-cached episode data
    _load_cached_data()
    sid = str(series_id)
    if sid in _cached_episodes:
        ep_data = _cached_episodes[sid]
        # Convert cached format to Xtream API format
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
        result = {
            "info": {
                "name": ep_data.get("name", ""),
                "cover": ep_data.get("cover", ""),
                "plot": ep_data.get("plot", ""),
                "genre": ep_data.get("genre", ""),
                "rating": ep_data.get("rating", ""),
                "releaseDate": ep_data.get("releaseDate", ""),
            },
            "episodes": episodes,
        }
        cache_set(key, result)
        return result
    # Fall back to API
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
    # Try pre-cached files
    data = _load_local_json("vod_list.json") or _load_github_json("vod_list.json")
    if data and isinstance(data, list):
        print(f"[Cache] Loaded {len(data)} VOD from cached files")
        cache_set("all_vod", data)
        return data
    if not USE_CACHED:
        try:
            data = _trim(api_call("get_vod_streams"), _VOD_FIELDS)
            cache_set("all_vod", data)
            return data
        except Exception as e:
            print(f"[API] Failed to load VOD: {e}")
    return []


def get_vod_info(vod_id: str) -> dict:
    key = f"vod_info_{vod_id}"
    cached = cache_get(key)
    if cached:
        return cached
    # Always try API for individual movie info (lightweight, single request)
    try:
        data = api_call("get_vod_info", f"&vod_id={vod_id}")
        cache_set(key, data)
        return data
    except Exception as e:
        print(f"[API] Failed to get VOD info {vod_id}: {e}")
    return {}


# ─── Stremio Manifest ───
MANIFEST = {
    "id": "com.anime3rb.xtream",
    "version": "1.0.0",
    "name": "Anime3rb أنمي",
    "description": "مشاهدة الأنمي من اشتراك anime3rb.vip عبر Stremio",
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
            "name": "أنمي - مسلسلات",
            "extra": [
                {"name": "search", "isRequired": False},
                {"name": "skip", "isRequired": False},
                {"name": "genre", "isRequired": False},
            ],
            "genres": ["أكشن", "كوميدي", "خيال", "شونين", "مغامرة", "دراما",
                       "خيال علمي", "سينين", "خارق للطبيعة", "غموض",
                       "إيسيكاي", "رياضي", "تاريخي", "ميكا"],
        },
        {
            "type": "series",
            "id": "anime3rb_new",
            "name": "أنمي - جديد",
            "extra": [
                {"name": "skip", "isRequired": False},
            ],
        },
        {
            "type": "series",
            "id": "anime3rb_popular",
            "name": "أنمي - الأكثر شعبية",
            "extra": [
                {"name": "skip", "isRequired": False},
            ],
        },
        {
            "type": "movie",
            "id": "anime3rb_movies",
            "name": "أنمي - أفلام",
            "extra": [
                {"name": "search", "isRequired": False},
                {"name": "skip", "isRequired": False},
            ],
        },
    ],
    "behaviorHints": {"configurable": False},
}


def _get_display_name(series_id: str, original_name: str) -> str:
    """Get English display name if available, otherwise return original."""
    return _english_names.get(str(series_id), original_name)


def _safe_int(val, default: int = 0) -> int:
    """Parse int from value that might be a range like '132 ~ 134'."""
    if isinstance(val, int):
        return val
    s = str(val).strip()
    if not s:
        return default
    # Extract first number from strings like '132 ~ 134'
    m = re.match(r'(\d+)', s)
    return int(m.group(1)) if m else default


def _match_all_words(words: list[str], *fields: str) -> bool:
    """Return True if every word appears in at least one of the fields."""
    combined = " ".join(f.lower() for f in fields if f)
    return all(w in combined for w in words)


# Common English/Arabic → romaji name aliases for better search
_SEARCH_ALIASES: dict[str, list[str]] = {
    # English aliases
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
    "the beginning after the end": ["the beginning after the end"],
    "overlord": ["overlord"],
    "no game no life": ["no game no life"],
    "steins gate": ["steins;gate", "steins gate"],
    # Arabic aliases
    "الخطايا السبع": ["nanatsu no taizai"],
    "الخطايا السبع المميتة": ["nanatsu no taizai"],
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
    "الكيميائي المعدني": ["fullmetal alchemist", "hagane no renkinjutsushi"],
    "بلو لوك": ["blue lock"],
    "فينلاند ساغا": ["vinland saga"],
    "بطل الدرع": ["tate no yuusha no nariagari"],
    "موشوكو تنسي": ["mushoku tensei"],
    "صعود المستوى": ["ore dake level up"],
    "برج الإله": ["kami no tou"],
}


def _expand_search(query: str) -> list[str]:
    """Expand a search query with aliases. Returns list of queries to try."""
    queries = [query]
    q_lower = query.lower().strip()
    for eng, aliases in _SEARCH_ALIASES.items():
        if eng in q_lower or q_lower in eng:
            queries.extend(aliases)
    return queries


def _search_score(query: str, name: str, plot: str) -> float:
    """Score how well a query matches a series/movie. Higher = better match.
    Returns 0 if no match at all."""
    q = query.lower().strip()
    name_lower = name.lower()
    plot_lower = plot.lower() if plot else ""

    # Exact name match
    if q == name_lower:
        return 100.0
    # Query is contained in name
    if q in name_lower:
        return 90.0
    # Name starts with query
    if name_lower.startswith(q):
        return 85.0

    # All words match in name
    words = q.split()
    if words and all(w in name_lower for w in words):
        return 80.0

    # All words match in name + plot
    combined = name_lower + " " + plot_lower
    if words and all(w in combined for w in words):
        return 60.0

    # Partial word match (at least 50% of words match in name)
    if len(words) >= 2:
        hits = sum(1 for w in words if w in name_lower)
        ratio = hits / len(words)
        if ratio >= 0.5:
            return 40.0 * ratio

    # Single word partial match in name
    if len(words) == 1 and len(q) >= 3 and q in name_lower:
        return 50.0

    return 0.0


# Cache TTL for TVDB and other lookups (24 hours)
KITSU_TTL = 24 * 60 * 60


# ─── TVDB API ───
def _tvdb_login() -> str:
    """Login to TVDB and return bearer token (cached for 25 days)."""
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
        print(f"[TVDB] Logged in successfully")
        return _tvdb_token
    except Exception as e:
        print(f"[TVDB] Login error: {e}")
        return ""


def _tvdb_get(path: str) -> dict | None:
    """Make an authenticated GET request to TVDB API."""
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


def _tvdb_search(anime_name: str, content_type: str = "series") -> int | None:
    """Search TVDB for an anime by name and return the TVDB ID."""
    tvdb_type = "movie" if content_type == "movie" else "series"
    key = f"tvdb_id_{tvdb_type}_{_normalize(anime_name)}"
    cached = cache_get(key, KITSU_TTL)
    if cached is not None:
        return cached if cached != 0 else None
    try:
        encoded = requests.utils.quote(anime_name)
        data = _tvdb_get(f"search?query={encoded}&type={tvdb_type}")
        if not data and tvdb_type == "movie":
            # Fallback: try without type filter for movies
            data = _tvdb_get(f"search?query={encoded}")
        if data:
            norm_name = _normalize(anime_name)
            # Exact name match
            for r in data:
                if _normalize(r.get("name", "")) == norm_name:
                    tvdb_id = int(r["tvdb_id"])
                    cache_set(key, tvdb_id)
                    return tvdb_id
            # Check aliases for a match
            for r in data:
                aliases = r.get("aliases", [])
                if isinstance(aliases, list):
                    for alias in aliases:
                        alias_name = alias if isinstance(alias, str) else alias.get("name", "")
                        if _normalize(alias_name) == norm_name:
                            tvdb_id = int(r["tvdb_id"])
                            cache_set(key, tvdb_id)
                            return tvdb_id
            # Fallback to first result
            tvdb_id = int(data[0]["tvdb_id"])
            cache_set(key, tvdb_id)
            return tvdb_id
        cache_set(key, 0)
        return None
    except Exception:
        cache_set(key, 0)
        return None


def _tvdb_artwork(tvdb_id: int, content_type: str = "series") -> dict:
    """Get artwork URLs from TVDB for a series or movie.
    Returns dict with keys: poster, background, logo (or empty strings)."""
    key = f"tvdb_art_{content_type}_{tvdb_id}"
    cached = cache_get(key, KITSU_TTL)
    if cached is not None:
        return cached

    result = {"poster": "", "background": "", "logo": ""}
    endpoint = "movies" if content_type == "movie" else "series"
    data = _tvdb_get(f"{endpoint}/{tvdb_id}/extended")
    if not data:
        cache_set(key, result)
        return result

    artworks = data.get("artworks", [])
    # Series art types: 2=poster, 3=background, 23=clearlogo
    # Movie art types: 14=poster, 15=background
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



def _normalize(text: str) -> str:
    """Remove punctuation and normalize for matching."""
    return re.sub(r'[^\w\s]', ' ', text.lower()).strip()



def _warm_cache() -> None:
    """Pre-fetch catalog lists in background so first requests are fast."""
    import gc
    try:
        time.sleep(2)  # Let server finish startup first
        print("[Cache] Warming: series categories...")
        get_series_categories()
        gc.collect()
        print("[Cache] Warming: all series list...")
        get_all_series()
        gc.collect()
        print("[Cache] Warming: all VOD list...")
        get_all_vod()
        gc.collect()
        # Pre-load episode data from cache
        print("[Cache] Loading pre-cached episode data...")
        _load_cached_data()
        gc.collect()
        print(f"[Cache] Warm-up complete! (episodes: {len(_cached_episodes)} series)")
    except Exception as e:
        print(f"[Cache] Warm-up error: {e}")


def _background_refresh() -> None:
    """Periodically refresh cached lists before they expire."""
    refresh_interval = 5 * 60 * 60  # Refresh every 5 hours (before 6h TTL expires)
    while True:
        time.sleep(refresh_interval)
        try:
            print("[Cache] Background refresh starting...")
            # Force refresh by clearing old cache for lists
            for key in ["series_categories", "all_series", "all_vod"]:
                with _lock:
                    _cache.pop(key, None)
                    _cache_ts.pop(key, None)
            get_series_categories()
            get_all_series()
            get_all_vod()
            # Reload episode data from GitHub/local
            global _cached_data_ts
            _cached_data_ts = 0  # Force reload
            _load_cached_data()
            print(f"[Cache] Background refresh complete! (episodes: {len(_cached_episodes)} series)")
        except Exception as e:
            print(f"[Cache] Background refresh error: {e}")


def load_genres() -> None:
    try:
        cats = get_series_categories()
        if isinstance(cats, list):
            MANIFEST["catalogs"][0]["genres"] = [c["category_name"] for c in cats]
    except Exception as e:
        print(f"[Genres] Error: {e}")


# ─── FastAPI App ───
app = FastAPI(title="Anime3rb Stremio Addon")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def stremio_response(data: dict) -> Response:
    return Response(
        content=json.dumps(data, ensure_ascii=False),
        media_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "cache_keys": list(_cache.keys()),
        "cache_size_mb": round(_cache_size_mb(), 1),
        "cached_episodes": len(_cached_episodes),
        "use_cached": USE_CACHED,
        "base_url": BASE_URL,
        "user": USERNAME[:3] + "***",
    }


@app.get("/test_api")
def test_api():
    results = {"use_cached": USE_CACHED, "cached_episodes": len(_cached_episodes)}
    url = f"{BASE_URL}/player_api.php?username={USERNAME}&password={PASSWORD}"
    if scraper:
        try:
            r = scraper.get(url, timeout=20)
            results["cloudscraper"] = {"status": r.status_code, "length": len(r.text), "ok": r.status_code == 200}
        except Exception as e:
            results["cloudscraper"] = {"error": str(e)}
    else:
        results["cloudscraper"] = {"error": "not installed"}
    try:
        r = requests.get(url, timeout=20)
        results["requests"] = {"status": r.status_code, "length": len(r.text), "ok": r.status_code == 200}
    except Exception as e:
        results["requests"] = {"error": str(e)}
    return results


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
        if content_type == "series" and catalog_id == "anime3rb_series":
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
                        # Search both original name and English name
                        eng_name = _english_names.get(sid, "")
                        name = s.get("name", "")
                        combined_name = f"{name} {eng_name}" if eng_name else name
                        score = _search_score(q, combined_name, s.get("plot", ""))
                        if score > 0 and (sid not in scored or score > scored[sid][0]):
                            scored[sid] = (score, s)
                ranked = sorted(scored.values(), key=lambda x: x[0], reverse=True)
                series = [s for _, s in ranked]

            if extras.get("genre"):
                genre_filter = unquote(extras["genre"])
                series = [
                    s for s in series
                    if genre_filter in [g.strip() for g in (s.get("genre") or "").split(",")]
                ]

            skip = int(extras.get("skip", 0))
            page = series[skip : skip + 100]

            metas = []
            for s in page:
                sid = str(s["series_id"])
                meta = {
                    "id": f"anime3rb_series_{sid}",
                    "type": "series",
                    "name": _get_display_name(sid, s.get("name", "")),
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

            return stremio_response({"metas": metas})

        if content_type == "series" and catalog_id == "anime3rb_new":
            series = get_all_series()
            if not isinstance(series, list):
                return stremio_response({"metas": []})
            # Sort by release date (newest first)
            series = sorted(
                [s for s in series if s.get("releaseDate")],
                key=lambda s: s.get("releaseDate", ""),
                reverse=True,
            )
            skip = int(extras.get("skip", 0))
            page = series[skip : skip + 100]
            metas = []
            for s in page:
                sid = str(s["series_id"])
                meta = {
                    "id": f"anime3rb_series_{sid}",
                    "type": "series",
                    "name": _get_display_name(sid, s.get("name", "")),
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
            return stremio_response({"metas": metas})

        if content_type == "series" and catalog_id == "anime3rb_popular":
            series = get_all_series()
            if not isinstance(series, list):
                return stremio_response({"metas": []})
            # Sort by rating (highest first)
            series = sorted(
                [s for s in series if s.get("rating")],
                key=lambda s: float(s.get("rating", 0) or 0),
                reverse=True,
            )
            skip = int(extras.get("skip", 0))
            page = series[skip : skip + 100]
            metas = []
            for s in page:
                sid = str(s["series_id"])
                meta = {
                    "id": f"anime3rb_series_{sid}",
                    "type": "series",
                    "name": _get_display_name(sid, s.get("name", "")),
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
            return stremio_response({"metas": metas})

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
            page = vod[skip : skip + 100]

            metas = []
            for v in page:
                meta = {
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

            return stremio_response({"metas": metas})

    except Exception as e:
        print(f"[Catalog] Error: {e}")

    return stremio_response({"metas": []})


@app.get("/meta/{content_type}/{meta_id}.json")
def meta(content_type: str, meta_id: str):
    try:
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

            # Try TVDB for background and logo (best source for anime)
            anime_name = info.get("name", "")
            tvdb_id = _tvdb_search(anime_name) if anime_name else None
            tvdb_art: dict = {}
            if tvdb_id:
                tvdb_art = _tvdb_artwork(tvdb_id)
                if tvdb_art.get("background"):
                    result["background"] = tvdb_art["background"]
                if tvdb_art.get("logo"):
                    result["logo"] = tvdb_art["logo"]

            videos = []
            cover = info.get("cover", "")
            if "episodes" in data:
                for season_num, episodes in data["episodes"].items():
                    for ep in episodes:
                        ep_num_raw = ep.get('episode_num', '')
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
            result = {
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

            # Enrich with TVDB artwork (background + logo)
            if movie_name:
                tvdb_id = _tvdb_search(movie_name, "movie")
                art_type = "movie"
                if not tvdb_id:
                    tvdb_id = _tvdb_search(movie_name, "series")
                    art_type = "series"
                if tvdb_id:
                    tvdb_art = _tvdb_artwork(tvdb_id, art_type)
                    if tvdb_art.get("background"):
                        result["background"] = tvdb_art["background"]
                    if tvdb_art.get("logo"):
                        result["logo"] = tvdb_art["logo"]

            return stremio_response({"meta": result})

    except Exception as e:
        print(f"[Meta] Error: {e}")

    return stremio_response({"meta": None})


@app.get("/stream/{content_type}/{stream_id}.json")
def stream(content_type: str, stream_id: str):
    try:
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

            return stremio_response(
                {
                    "streams": [
                        {
                            "url": stream_url,
                            "title": f"{ep.get('title', 'الحلقة ' + episode_num)}\n{ep.get('duration', '')}",
                            "name": "Anime3rb",
                            "behaviorHints": {"notWebReady": True},
                        }
                    ]
                }
            )

        if content_type == "movie" and stream_id.startswith("anime3rb_vod_"):
            vod_id = stream_id.replace("anime3rb_vod_", "")
            all_vod = get_all_vod()
            vod_item = next(
                (v for v in all_vod if str(v.get("stream_id")) == vod_id), None
            ) if isinstance(all_vod, list) else None
            ext = (vod_item.get("container_extension") if vod_item else None) or "mp4"
            stream_url = f"{BASE_URL}/movie/{USERNAME}/{PASSWORD}/{vod_id}.{ext}"

            return stremio_response(
                {
                    "streams": [
                        {
                            "url": stream_url,
                            "title": vod_item.get("name", "Anime3rb") if vod_item else "Anime3rb",
                            "name": "Anime3rb",
                            "behaviorHints": {"notWebReady": True},
                        }
                    ]
                }
            )


    except Exception as e:
        print(f"[Stream] Error: {e}")

    return stremio_response({"streams": []})


@app.on_event("startup")
def startup():
    print("[Addon] Loading genres...")
    load_genres()
    # Warm cache in background thread so server starts fast
    t = threading.Thread(target=_warm_cache, daemon=True)
    t.start()
    # Background refresh thread keeps cache fresh before TTL expires
    r = threading.Thread(target=_background_refresh, daemon=True)
    r.start()
    print(f"[Addon] Ready! Install: http://localhost:8000/manifest.json")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
