"""Live IMDb -> anime3rb resolver on TMDB numbering.

Nothing here depends on a pre-built map, so brand-new anime and brand-new
seasons resolve the moment anime3rb lists them.

anime3rb lists every season/cour as its own series (Japanese style); TMDB groups
them as seasons of one show. For a request (imdb, S, E):
  1. TMDB: imdb -> show -> every title variant + every season's episode list.
  2. anime3rb series (TV/ONA) whose name is one of those titles (optionally
     followed by a season marker / subtitle) are the show's parts.
  3. Each part is assigned to the TMDB season whose air window contains its
     release date. Parts of one season are concatenated in release order.
  4. A season without its own part continues the most recent earlier part
     (long absolute-numbered series such as One Piece / Conan).
"""
import json
import os
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

TMDB_KEY = os.environ.get("TMDB_API_KEY", "")
TMDB_BASE = "https://api.themoviedb.org/3"

SERIES_CATEGORIES = {"1", "4"}  # TV series, ONA
LIST_TTL = 900
LIST_MIN_AGE_FOR_REFRESH = 120
TMDB_TTL = 6 * 3600
INFO_TTL = 300
WINDOW_BEFORE = timedelta(days=21)
WINDOW_AFTER = timedelta(days=3)
EARLY_RELEASE = timedelta(days=90)

_cache: dict = {}


class Unresolved(Exception):
    """Live resolution could not map the request; caller may try other sources."""


def _cached(key, ttl, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    val = fn()
    if val is not None:
        _cache[key] = (now + ttl, val)
    return val


# ─── Name normalization ───
_SEP = re.compile(r"\s*(?:[:?!~|(\[]|\s[-–—]\s)\s*")
_SEASONISH = re.compile(
    r"^(?:season|part|cour|\d+|[ivx]+|s\d+|second|third|fourth|fifth|sixth|"
    r"final|the final|last|zoku|kan|act)\b")


def loose(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = s.replace("&", " and ").replace("×", " x ")
    s = re.sub("[أإآٱ]", "ا", s).replace("ة", "ه").replace("ى", "ي").replace("ـ", "")
    s = re.sub(r"(\d+)(?:st|nd|rd|th)\b", r"\1", s)
    s = re.sub(r"[\W_]+", " ", s)
    s = s.replace("ou", "o").replace("uu", "u").replace("oo", "o")
    return re.sub(r"\s+", " ", s).strip()


def _usable(v: str) -> bool:
    return len(v) >= 3 and not v.isdigit()


def _name_matches(variant: str, full: str, head: str) -> bool:
    if variant in (full, head):
        return True
    for x in (full, head):
        if x.startswith(variant + " "):
            rem = x[len(variant) + 1:]
            if _SEASONISH.match(rem) or rem == "hen" or rem.endswith(" hen"):
                return True
    return False


def _parse_date(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


# ─── TMDB ───
def _tmdb(path: str, **params):
    params["api_key"] = TMDB_KEY
    url = f"{TMDB_BASE}{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {}
            if e.code != 429 or attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(1 + attempt)
    return {}


def _titles(d: dict, name_key: str) -> list:
    out = [d.get(name_key), d.get("original_" + name_key)]
    alts = d.get("alternative_titles") or {}
    for a in alts.get("results") or alts.get("titles") or []:
        out.append(a.get("title"))
    for t in (d.get("translations") or {}).get("translations", []):
        out.append((t.get("data") or {}).get(name_key))
    seen, res = set(), []
    for t in out:
        v = loose(t or "")
        if _usable(v) and v not in seen:
            seen.add(v)
            res.append(v)
    return res


def _tmdb_find(imdb: str) -> dict:
    return _cached(("find", imdb), TMDB_TTL,
                   lambda: _tmdb(f"/find/{imdb}", external_source="imdb_id"))


def _tmdb_show(tv_id: int) -> dict:
    def load():
        d = _tmdb(f"/tv/{tv_id}", append_to_response="alternative_titles,translations")
        if not d:
            return None
        nums = [s["season_number"] for s in d.get("seasons", []) if s.get("season_number")]
        seasons = {}
        for i in range(0, len(nums), 20):
            chunk = nums[i:i + 20]
            sd = _tmdb(f"/tv/{tv_id}",
                       append_to_response=",".join(f"season/{n}" for n in chunk))
            for n in chunk:
                eps = (sd.get(f"season/{n}") or {}).get("episodes") or []
                seasons[n] = eps
        return {"titles": _titles(d, "name"), "seasons": seasons,
                "ongoing": bool(d.get("in_production"))}
    return _cached(("tv", tv_id), TMDB_TTL, load)


def _tmdb_movie(movie_id: int) -> dict:
    def load():
        d = _tmdb(f"/movie/{movie_id}", append_to_response="alternative_titles,translations")
        return {"titles": _titles(d, "title")} if d else None
    return _cached(("movie", movie_id), TMDB_TTL, load)


# ─── anime3rb catalog ───
class Catalog:
    def __init__(self, api_call):
        self.api_call = api_call
        self._series = None
        self._series_at = 0.0
        self._vod = None
        self._vod_at = 0.0

    def _load_series(self):
        items = []
        for s in self.api_call("get_series", timeout=60) or []:
            if str(s.get("category_id")) not in SERIES_CATEGORIES:
                continue
            name = s.get("name") or ""
            items.append({
                "id": str(s.get("series_id")),
                "name": name,
                "full": loose(name),
                "head": loose(_SEP.split(name, maxsplit=1)[0]),
                "date": _parse_date(s.get("releaseDate")),
            })
        return items

    def series(self, force: bool = False):
        now = time.time()
        stale = now - self._series_at > LIST_TTL
        if self._series is None or stale or (
                force and now - self._series_at > LIST_MIN_AGE_FOR_REFRESH):
            self._series = self._load_series()
            self._series_at = now
        return self._series

    def vod(self, force: bool = False):
        now = time.time()
        if self._vod is None or now - self._vod_at > LIST_TTL or (
                force and now - self._vod_at > LIST_MIN_AGE_FOR_REFRESH):
            self._vod = [{"id": str(v.get("stream_id")), "full": loose(v.get("name") or "")}
                         for v in self.api_call("get_vod_streams", timeout=60) or []]
            self._vod_at = now
        return self._vod


def _season_windows(seasons: dict, ongoing: bool) -> list:
    """[(season_number, start, end)] for regular seasons that have aired/are dated."""
    rows = []
    for n in sorted(seasons):
        dates = [_parse_date(e.get("air_date")) for e in seasons[n]]
        dates = [d for d in dates if d]
        if dates:
            rows.append([n, min(dates), max(dates)])
    out = []
    for i, (n, first, last) in enumerate(rows):
        start = first - WINDOW_BEFORE
        end = last + WINDOW_AFTER
        if i + 1 < len(rows):
            end = min(end, rows[i + 1][1] - WINDOW_BEFORE)
        elif ongoing:
            end = max(end, date.today() + timedelta(days=60))
        out.append((n, start, end))
    return out


def _parts_by_season(show: dict, catalog: Catalog, force: bool, known=()):
    titles = show["titles"]
    known = {str(k) for k in known or ()}
    cands, side = [], set()
    for s in catalog.series(force):
        if not s["date"]:
            continue
        if s["id"] in known or any(_name_matches(v, s["full"], s["full"]) for v in titles):
            cands.append(s)
        elif any(_name_matches(v, s["full"], s["head"]) for v in titles):
            cands.append(s)  # "<title>: <arc/spin-off subtitle>"
            side.add(s["id"])
    windows = _season_windows(show["seasons"], show.get("ongoing", True))
    parts = {}
    for c in cands:
        n = next((n for n, start, end in windows if start <= c["date"] <= end), None)
        if n is None:
            # Streamed early (e.g. Netflix ONA weeks before the TV broadcast TMDB dates).
            n = next((n for n, start, end in windows
                      if c["date"] < start <= c["date"] + EARLY_RELEASE), None)
        if n is not None:
            parts.setdefault(n, []).append(c)
    for n in parts:
        # Main/season-numbered entries first; subtitle-only entries follow them.
        parts[n].sort(key=lambda c: (c["id"] in side, c["date"],
                                     int(c["id"]) if c["id"].isdigit() else 0))
        uniq = {}
        for c in parts[n]:
            uniq.setdefault(c["full"], []).append(c)
        parts[n] = [g[0] if len(g) == 1 else g for g in uniq.values()]
    return parts, bool(cands)


def _aired(eps: list) -> int:
    today = date.today()
    return sum(1 for e in eps if (_parse_date(e.get("air_date")) or date.max) <= today)


def _first_num(eps: list) -> int:
    if not eps:
        return 1
    m = re.match(r"\s*(\d+)", str(eps[0].get("episode_num", "1")))
    return int(m.group(1)) if m else 1


class _Part:
    """One anime3rb entry with its local episode numbering."""

    def __init__(self, info, eps_in_order):
        self.info = info
        eps = eps_in_order(info)
        first = _first_num(eps)
        # Entries that continue the show's numbering (e.g. 2nd season starting at 25).
        self.base = first - 1 if first > 5 else 0
        nums = []
        for e in eps:
            m = re.match(r"\s*(\d+)", str(e.get("episode_num", "")))
            if m:
                nums.append(int(m.group(1)))
        self.size = max(len(eps), (max(nums) - self.base) if nums else 0)


def _load_part(cand, series_info, eps_in_order):
    group = cand if isinstance(cand, list) else [cand]
    best = None
    for c in group:  # duplicate listings of one part: keep the most complete
        info = _cached(("info", c["id"]), INFO_TTL, lambda: series_info(c["id"]))
        if info:
            p = _Part(info, eps_in_order)
            if best is None or p.size > best.size:
                best = p
    if best is None:
        raise Unresolved("anime3rb series_info failed")
    return best


def _position_in_season(seasons: dict, season: int, episode: int):
    """(season, 1-based position) per TMDB, accepting within-season or absolute numbers."""
    eps = seasons.get(season)
    if not eps:
        return None
    for i, e in enumerate(eps, 1):
        if e.get("episode_number") == episode:
            return season, i
    if 1 <= episode <= len(eps):
        return season, episode
    # Absolute episode number sent under a season label.
    remaining = episode
    for n in sorted(k for k in seasons if k > 0):
        cnt = len(seasons[n])
        if remaining <= cnt:
            return n, remaining
        remaining -= cnt
    return None


def resolve_series(imdb: str, season: int, episode: int, catalog: Catalog,
                   series_info, find_by_epnum, episodes_in_order, known=()):
    """Return (episode_dict, series_info_data) or None if not (yet) available.
    Raises Unresolved when the show can't be mapped live."""
    if not TMDB_KEY:
        raise Unresolved("no TMDB key")
    if season <= 0:
        return None
    tv = (_tmdb_find(imdb) or {}).get("tv_results") or []
    if not tv:
        raise Unresolved("imdb not on TMDB")
    show = _tmdb_show(tv[0]["id"])
    if not show:
        raise Unresolved("TMDB show missing")
    seasons = show["seasons"]
    pos = _position_in_season(seasons, season, episode)
    if not pos:
        return None
    season, idx = pos

    parts, any_cand = _parts_by_season(show, catalog, False, known)
    if season not in parts:
        parts, any_cand = _parts_by_season(show, catalog, True, known)
    if not any_cand:
        raise Unresolved("no anime3rb series matches")

    loaded = {}

    def part(c):
        key = id(c)
        if key not in loaded:
            loaded[key] = _load_part(c, series_info, episodes_in_order)
        return loaded[key]

    # Walk seasons in order, tracking where each anime3rb part starts on the TMDB
    # absolute timeline. A season whose episodes an earlier long-running part
    # already covers (One Piece) keeps that part; side entries listed in its
    # window (recaps/remakes) are ignored.
    order = sorted(n for n in seasons if n > 0)
    needed = [c for n in order if n <= season for c in parts.get(n) or []]
    if len(needed) > 1:
        with ThreadPoolExecutor(max_workers=min(8, len(needed))) as ex:
            list(ex.map(part, needed))
    cum = 0
    anchor = None  # (part, absolute start)
    for n in order:
        group = parts.get(n) or []
        covered = False
        if anchor and group:
            ext = anchor[1] + part(anchor[0]).size - cum
            covered = ext >= max(2, _aired(seasons[n]))
        if n == season:
            target = cum + idx
            if group and not covered:
                start = cum
                for c in group:
                    p = part(c)
                    if target <= start + p.size:
                        return find_by_epnum(p.info, p.base + target - start), p.info
                    start += p.size
                last = part(group[-1])
                return find_by_epnum(last.info, last.base + target - start + last.size), last.info
            if not anchor:
                raise Unresolved("no anime3rb part for season")
            p = part(anchor[0])
            ep = find_by_epnum(p.info, p.base + target - anchor[1])
            if ep:
                return ep, p.info
            raise Unresolved("episode beyond anchor part")
        if group and not covered:
            start = cum
            for c in group[:-1]:
                start += part(c).size
            anchor = (group[-1], start)
        cum += len(seasons[n])
    return None


def resolve_movie(imdb: str, catalog: Catalog):
    """Return anime3rb VOD id or None."""
    if not TMDB_KEY:
        return None
    mv = (_tmdb_find(imdb) or {}).get("movie_results") or []
    if not mv:
        return None
    m = _tmdb_movie(mv[0]["id"])
    if not m:
        return None
    for force in (False, True):
        hits = {v["id"] for v in catalog.vod(force) if v["full"] in m["titles"]}
        if len(hits) == 1:
            return hits.pop()
        if len(hits) > 1:
            return None
    return None
