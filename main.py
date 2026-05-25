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

import requests
import cloudscraper
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

# ─── Configuration ───
BASE_URL = os.environ.get("ANIME3RB_URL", "https://anime3rb.vip")
USERNAME = os.environ.get("ANIME3RB_USER", "bbnmnbb")
PASSWORD = os.environ.get("ANIME3RB_PASS", "as209509")

# ─── Cloudscraper session ───
scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "linux"})

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


# ─── Xtream API ───
def api_call(action: str = "", extra: str = "", timeout: int = 60) -> Any:
    url = f"{BASE_URL}/player_api.php?username={USERNAME}&password={PASSWORD}"
    if action:
        url += f"&action={action}"
    if extra:
        url += extra
    try:
        r = scraper.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[API] cloudscraper failed: {e}, trying requests...")
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()


def get_series_categories() -> list[dict]:
    cached = cache_get("series_categories")
    if cached:
        return cached
    data = api_call("get_series_categories")
    cache_set("series_categories", data)
    return data


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
    data = api_call("get_series_info", f"&series_id={series_id}", timeout=120)
    cache_set(key, data)
    return data


def get_all_vod() -> list[dict]:
    cached = cache_get("all_vod")
    if cached:
        return cached
    data = _trim(api_call("get_vod_streams"), _VOD_FIELDS)
    cache_set("all_vod", data)
    return data


def get_vod_info(vod_id: str) -> dict:
    key = f"vod_info_{vod_id}"
    cached = cache_get(key)
    if cached:
        return cached
    data = api_call("get_vod_info", f"&vod_id={vod_id}")
    cache_set(key, data)
    return data


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
            "idPrefixes": ["anime3rb_", "kitsu:", "tt", "mal:"],
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
            "genres": [],
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


# ─── External ID lookup ───
KITSU_TTL = 24 * 60 * 60  # Also used for MAL and Cinemeta lookups  # 24h cache for Kitsu lookups


def _fetch_kitsu_titles(kitsu_id: str) -> list[str]:
    """Get all title variants from Kitsu API for matching."""
    key = f"kitsu_titles_{kitsu_id}"
    cached = cache_get(key, KITSU_TTL)
    if cached is not None:
        return cached
    try:
        r = requests.get(
            f"https://kitsu.io/api/edge/anime/{kitsu_id}",
            headers={"Accept": "application/vnd.api+json"},
            timeout=10,
        )
        r.raise_for_status()
        attrs = r.json().get("data", {}).get("attributes", {})
        titles = set()
        for t in (attrs.get("titles") or {}).values():
            if t:
                titles.add(t)
        if attrs.get("canonicalTitle"):
            titles.add(attrs["canonicalTitle"])
        for t in attrs.get("abbreviatedTitles") or []:
            if t:
                titles.add(t)
        result = list(titles)
        cache_set(key, result)
        return result
    except Exception as e:
        print(f"[Kitsu] Error fetching {kitsu_id}: {e}")
        return []


def _fetch_cinemeta_meta(imdb_id: str, content_type: str) -> dict | None:
    """Get full meta from Cinemeta (cached). Returns the meta dict or None."""
    key = f"cinemeta_meta_{imdb_id}"
    cached = cache_get(key, KITSU_TTL)
    if cached is not None:
        return cached
    try:
        r = requests.get(
            f"https://v3-cinemeta.strem.io/meta/{content_type}/{imdb_id}.json",
            timeout=15,
        )
        r.raise_for_status()
        meta = r.json().get("meta", {})
        cache_set(key, meta)
        return meta
    except Exception as e:
        print(f"[Cinemeta] Error fetching {imdb_id}: {e}")
        return None


def _title_close(a: str, b: str) -> bool:
    """Check if two normalized titles are close enough to be the same show."""
    if a == b:
        return True
    if a in b or b in a:
        return len(a) / max(len(b), 1) > 0.5 and len(b) / max(len(a), 1) > 0.5
    return False


def _fetch_cinemeta_name(imdb_id: str, content_type: str) -> list[str]:
    """Get title from Cinemeta, enriched with Kitsu alternatives for better matching."""
    meta = _fetch_cinemeta_meta(imdb_id, content_type)
    if not meta or not meta.get("name"):
        return []
    cinemeta_name = meta["name"]
    titles = [cinemeta_name]
    # Search Kitsu for alternative titles (e.g. "Detective Conan" → "Meitantei Conan")
    norm_cinemeta = _normalize(cinemeta_name)
    try:
        r = requests.get(
            f"https://kitsu.io/api/edge/anime?filter[text]={cinemeta_name}&page[limit]=3",
            headers={"Accept": "application/vnd.api+json"},
            timeout=10,
        )
        r.raise_for_status()
        for item in r.json().get("data", []):
            attrs = item.get("attributes", {})
            # Only use Kitsu result if at least one of its titles closely matches Cinemeta name
            all_titles = list((attrs.get("titles") or {}).values())
            ct = attrs.get("canonicalTitle")
            if ct:
                all_titles.append(ct)
            is_relevant = any(
                _title_close(norm_cinemeta, _normalize(t))
                for t in all_titles if t
            )
            if not is_relevant:
                continue
            for t in all_titles:
                if t and t not in titles:
                    titles.append(t)
    except Exception:
        pass
    return titles


def _cinemeta_absolute_ep(imdb_id: str, season: int, episode: int) -> int:
    """Convert Cinemeta season+episode to absolute episode number."""
    meta = _fetch_cinemeta_meta(imdb_id, "series")
    if not meta:
        return episode  # Fallback to raw episode number

    videos = meta.get("videos", [])
    # Filter to real episodes (season > 0) and sort
    real = [v for v in videos if v.get("season", 0) > 0]
    real.sort(key=lambda v: (v.get("season", 0), v.get("episode", 0)))

    # Count position to find absolute number
    abs_num = 0
    for v in real:
        abs_num += 1
        if v.get("season") == season and v.get("episode") == episode:
            return abs_num

    return episode  # Fallback if not found


def _fetch_mal_titles(mal_id: str) -> list[str]:
    """Get all title variants from MyAnimeList via Jikan API."""
    key = f"mal_titles_{mal_id}"
    cached = cache_get(key, KITSU_TTL)
    if cached is not None:
        return cached
    try:
        r = requests.get(f"https://api.jikan.moe/v4/anime/{mal_id}", timeout=10)
        r.raise_for_status()
        data = r.json().get("data", {})
        titles = set()
        if data.get("title"):
            titles.add(data["title"])
        if data.get("title_english"):
            titles.add(data["title_english"])
        if data.get("title_japanese"):
            titles.add(data["title_japanese"])
        for t in data.get("title_synonyms", []):
            if t:
                titles.add(t)
        for t_obj in data.get("titles", []):
            if t_obj.get("title"):
                titles.add(t_obj["title"])
        result = list(titles)
        cache_set(key, result)
        return result
    except Exception as e:
        print(f"[MAL] Error fetching {mal_id}: {e}")
        return []


def _normalize(text: str) -> str:
    """Remove punctuation and normalize for matching."""
    return re.sub(r'[^\w\s]', ' ', text.lower()).strip()


def _word_score(title_words: list[str], name: str) -> float:
    """Fraction of title_words found in name."""
    if not title_words:
        return 0.0
    name_lower = _normalize(name)
    hits = sum(1 for w in title_words if w in name_lower)
    return hits / len(title_words)


def _best_match(titles: list[str], items: list[dict], name_key: str = "name") -> dict | None:
    """Find the best matching item by trying exact, all-words, then scoring."""
    # Pass 1: exact match
    for title in titles:
        norm_title = _normalize(title)
        for item in items:
            if _normalize(item.get(name_key, "")) == norm_title:
                return item

    # Pass 2: all-words match
    for title in titles:
        words = _normalize(title).split()
        if not words:
            continue
        for item in items:
            if _match_all_words(words, item.get(name_key, "")):
                return item

    # Pass 3: best score (at least 60% of words match)
    best_item = None
    best_score = 0.0
    for title in titles:
        words = _normalize(title).split()
        if len(words) < 2:
            continue
        for item in items:
            score = _word_score(words, item.get(name_key, ""))
            if score > best_score and score >= 0.6:
                best_score = score
                best_item = item

    return best_item


def _find_series_by_name(titles: list[str]) -> dict | None:
    """Find the best matching anime3rb series for the given title list."""
    try:
        all_series = get_all_series()
        if not isinstance(all_series, list):
            return None
    except Exception:
        return None
    return _best_match(titles, all_series)


def _find_vod_by_name(titles: list[str]) -> dict | None:
    """Find the best matching anime3rb VOD (movie) for the given title list."""
    try:
        all_vod = get_all_vod()
        if not isinstance(all_vod, list):
            return None
    except Exception:
        return None
    return _best_match(titles, all_vod)


def _stream_for_external_series(stream_id: str) -> list[dict]:
    """Resolve stream for an external series episode ID (kitsu:ID:ep or tt...:S:E)."""
    if stream_id.startswith("kitsu:"):
        parts = stream_id.split(":")
        if len(parts) < 3:
            return []
        kitsu_id = parts[1]
        ep_num = _safe_int(parts[2])
        titles = _fetch_kitsu_titles(kitsu_id)
    elif stream_id.startswith("mal:"):
        parts = stream_id.split(":")
        if len(parts) < 3:
            return []
        mal_id = parts[1]
        ep_num = _safe_int(parts[2])
        titles = _fetch_mal_titles(mal_id)
    elif stream_id.startswith("tt"):
        parts = stream_id.split(":")
        imdb_id = parts[0]
        # Check if this is actually animation - skip non-anime content
        meta = _fetch_cinemeta_meta(imdb_id, "series")
        if meta:
            genres = [g.lower() for g in meta.get("genres", [])]
            if "animation" not in genres:
                return []
        season = _safe_int(parts[1]) if len(parts) >= 2 else 1
        raw_ep = _safe_int(parts[2]) if len(parts) >= 3 else 0
        ep_num = _cinemeta_absolute_ep(imdb_id, season, raw_ep)
        print(f"[Stream] IMDB {imdb_id} s{season}e{raw_ep} → absolute ep {ep_num}")
        titles = _fetch_cinemeta_name(imdb_id, "series")
    else:
        return []

    if not titles:
        return []

    match = _find_series_by_name(titles)
    if not match:
        return []

    series_id = str(match["series_id"])
    try:
        data = get_series_info(series_id)
    except Exception as e:
        print(f"[Stream] Error getting series info {series_id}: {e}")
        return []

    if not data or "episodes" not in data:
        return []

    # Find the episode by number across all seasons
    for season_num, episodes in data["episodes"].items():
        for ep in episodes:
            if _safe_int(ep.get("episode_num")) == ep_num:
                ext = ep.get("container_extension", "mp4")
                stream_url = f"{BASE_URL}/series/{USERNAME}/{PASSWORD}/{ep['stream_id']}.{ext}"
                return [
                    {
                        "url": stream_url,
                        "title": f"{match.get('name', 'Anime3rb')}\n{ep.get('title', f'Episode {ep_num}')}",
                        "name": "Anime3rb",
                        "behaviorHints": {"notWebReady": True},
                    }
                ]
    return []


def _stream_for_external_movie(stream_id: str) -> list[dict]:
    """Resolve stream for an external movie ID (kitsu:ID, mal:ID, or ttXXX)."""
    if stream_id.startswith("kitsu:"):
        kitsu_id = stream_id.split(":")[1]
        titles = _fetch_kitsu_titles(kitsu_id)
    elif stream_id.startswith("mal:"):
        mal_id = stream_id.split(":")[1]
        titles = _fetch_mal_titles(mal_id)
    elif stream_id.startswith("tt"):
        imdb_id = stream_id.split(":")[0]
        meta = _fetch_cinemeta_meta(imdb_id, "movie")
        if meta:
            genres = [g.lower() for g in meta.get("genres", [])]
            if "animation" not in genres:
                return []
        titles = _fetch_cinemeta_name(imdb_id, "movie")
    else:
        return []

    if not titles:
        return []

    match = _find_vod_by_name(titles)
    if not match:
        return []

    vod_id = str(match.get("stream_id", ""))
    ext = match.get("container_extension", "mp4")
    stream_url = f"{BASE_URL}/movie/{USERNAME}/{PASSWORD}/{vod_id}.{ext}"
    return [
        {
            "url": stream_url,
            "title": match.get("name", "Anime3rb"),
            "name": "Anime3rb",
            "behaviorHints": {"notWebReady": True},
        }
    ]


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
        print("[Cache] Warm-up complete!")
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
            print("[Cache] Background refresh complete!")
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
    return {"status": "ok", "cache_keys": list(_cache.keys()), "cache_size_mb": round(_cache_size_mb(), 1)}


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
                q = unquote(extras["search"]).lower()
                words = q.split()
                series = [
                    s
                    for s in series
                    if _match_all_words(words, s.get("name", ""), s.get("plot", ""))
                ]

            if extras.get("genre"):
                cats = get_series_categories()
                cat = next(
                    (c for c in cats if c["category_name"] == extras["genre"]), None
                )
                if cat:
                    series = [
                        s
                        for s in series
                        if str(s.get("category_id")) == str(cat["category_id"])
                    ]

            skip = int(extras.get("skip", 0))
            page = series[skip : skip + 100]

            metas = []
            for s in page:
                meta = {
                    "id": f"anime3rb_series_{s['series_id']}",
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

            return stremio_response({"metas": metas})

        if content_type == "movie" and catalog_id == "anime3rb_movies":
            vod = get_all_vod()
            if not isinstance(vod, list):
                return stremio_response({"metas": []})

            if extras.get("search"):
                q = unquote(extras["search"]).lower()
                words = q.split()
                vod = [
                    v
                    for v in vod
                    if _match_all_words(words, v.get("name", ""), v.get("plot", ""))
                ]

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

            videos = []
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

            result = {
                "id": meta_id,
                "type": "movie",
                "name": info.get("name") or info.get("movie_name", ""),
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

        # ─── External ID stream handling (Kitsu, IMDB) ───
        if stream_id.startswith("kitsu:") or stream_id.startswith("mal:") or stream_id.startswith("tt"):
            if content_type == "series":
                streams = _stream_for_external_series(stream_id)
            else:
                streams = _stream_for_external_movie(stream_id)
            return stremio_response({"streams": streams})

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
