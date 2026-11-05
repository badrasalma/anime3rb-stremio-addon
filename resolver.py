"""Live IMDb -> anime3rb resolver on TVDB (aired order) numbering.

Nothing here depends on a pre-built map, so brand-new anime and brand-new
seasons resolve the moment anime3rb lists them.

anime3rb lists every season/cour as its own series (Japanese style); TVDB groups
them as seasons of one show. For a request (imdb, S, E):
  1. TVDB: imdb -> show -> every title variant (plus TMDB's) + every season's
     episode list.
  2. anime3rb series (TV/ONA) whose name is one of those titles (optionally
     followed by a season marker / subtitle) are the show's parts.
  3. Each part is assigned to the TVDB season whose air window contains its
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
TVDB_KEY = os.environ.get("TVDB_API_KEY", "")
TVDB_BASE = "https://api4.thetvdb.com/v4"

SERIES_CATEGORIES = {"1", "4"}  # TV series, ONA
LIST_TTL = 900
LIST_MIN_AGE_FOR_REFRESH = 120
TMDB_TTL = 6 * 3600
INFO_TTL = 300
WINDOW_BEFORE = timedelta(days=21)
WINDOW_AFTER = timedelta(days=3)
EARLY_RELEASE = timedelta(days=90)
LATE_LISTING = timedelta(days=365)

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


_SEQUEL_TAG = re.compile(r"[rsz]|r\d*|plus|er|lv \d+|vs .+")  # Sailor Moon R/S, Lv.2, "vs. U-20"


def _seasonish(rem: str) -> bool:
    return (bool(_SEASONISH.match(rem)) or rem == "hen" or rem.endswith(" hen")
            or bool(_SEQUEL_TAG.fullmatch(rem)))


def _name_matches(variant: str, full: str, head: str) -> bool:
    if variant in (full, head):
        return True
    vn = variant.replace(" ", "")
    for x in (full, head):
        if x.startswith(variant + " ") and _seasonish(x[len(variant) + 1:]):
            return True
        # Romanization spacing differs between sources ("shichau zo" / "shichauzo").
        if not x.replace(" ", "").startswith(vn):
            continue
        if x.replace(" ", "") == vn:
            return True
        i = x.find(" ")
        while i != -1:
            if x[:i].replace(" ", "") == vn:
                if _seasonish(x[i + 1:]):
                    return True
                break
            i = x.find(" ", i + 1)
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


def _dedup_titles(out) -> list:
    seen, res = set(), []
    for t in out:
        v = loose(t or "")
        if _usable(v) and v not in seen:
            seen.add(v)
            res.append(v)
    return res


def _titles(d: dict, name_key: str) -> list:
    out = [d.get(name_key), d.get("original_" + name_key)]
    alts = d.get("alternative_titles") or {}
    for a in alts.get("results") or alts.get("titles") or []:
        out.append(a.get("title"))
    for t in (d.get("translations") or {}).get("translations", []):
        out.append((t.get("data") or {}).get(name_key))
    return _dedup_titles(out)


def _tmdb_find(imdb: str) -> dict:
    return _cached(("find", imdb), TMDB_TTL,
                   lambda: _tmdb(f"/find/{imdb}", external_source="imdb_id"))


def _tmdb_tv_titles(imdb: str) -> list:
    """Extra title variants from TMDB (often has Arabic/romaji names TVDB lacks)."""
    if not TMDB_KEY:
        return []
    try:
        tv = (_tmdb_find(imdb) or {}).get("tv_results") or []
        if not tv:
            return []
        tv_id = tv[0]["id"]
        return _cached(("tvtitles", tv_id), TMDB_TTL, lambda: _titles(
            _tmdb(f"/tv/{tv_id}", append_to_response="alternative_titles,translations"),
            "name")) or []
    except Exception as e:
        print(f"[Resolver] TMDB titles failed for {imdb}: {e}")
        return []


# ─── TVDB ───
_tvdb_token = {"value": "", "at": 0.0}
TVDB_TOKEN_TTL = 20 * 86400


def _tvdb_login(force: bool = False) -> str:
    now = time.time()
    if force or not _tvdb_token["value"] or now - _tvdb_token["at"] > TVDB_TOKEN_TTL:
        req = urllib.request.Request(
            f"{TVDB_BASE}/login", data=json.dumps({"apikey": TVDB_KEY}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            _tvdb_token["value"] = json.load(r)["data"]["token"]
        _tvdb_token["at"] = now
    return _tvdb_token["value"]


def _tvdb(path: str, **params):
    url = f"{TVDB_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    relogin = False
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url, headers={"Authorization": "Bearer " + _tvdb_login(relogin)})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {}
            relogin = e.code == 401
            if e.code not in (401, 429) and e.code < 500 or attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            relogin = False
            if attempt == 2:
                raise
        time.sleep(1 + attempt)
    return {}


def _tvdb_find(imdb: str, hint=None):
    def load():
        for r in (_tvdb(f"/search/remoteid/{imdb}") or {}).get("data") or []:
            sid = (r.get("series") or {}).get("id")
            if sid:
                return int(sid)
        return 0
    sid = _cached(("tvdbfind", imdb), TMDB_TTL, load)
    return sid or (int(hint) if hint else 0)


def _tvdb_show(tvdb_id: int) -> dict:
    """{"titles", "seasons": {n: [{"episode_number", "air_date"}]}, "ongoing"}."""
    def load():
        d = (_tvdb(f"/series/{tvdb_id}/extended", meta="translations", short="true")
             or {}).get("data")
        if not d:
            return None
        tr = d.get("translations") or {}
        names = [d.get("name")]
        names += [t.get("name") for t in tr.get("nameTranslations") or []]
        names += [a.get("name") for a in tr.get("alias") or []]
        names += [a.get("name") for a in d.get("aliases") or []]
        seasons = {}
        page = 0
        while page < 20:
            r = _tvdb(f"/series/{tvdb_id}/episodes/default", page=page) or {}
            for e in (r.get("data") or {}).get("episodes") or []:
                n = e.get("seasonNumber") or 0
                if n > 0:
                    seasons.setdefault(n, []).append(
                        {"episode_number": e.get("number"), "air_date": e.get("aired")})
            if not (r.get("links") or {}).get("next"):
                break
            page += 1
        for n in seasons:
            seasons[n].sort(key=lambda e: e["episode_number"] or 0)
        status = ((d.get("status") or {}).get("name") or "").lower()
        return {"titles": _dedup_titles(names), "seasons": seasons,
                "ongoing": status in ("continuing", "upcoming", "")}
    return _cached(("tvdb", tvdb_id), TMDB_TTL, load)


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
    parts, late = {}, []
    for c in cands:
        n = next((n for n, start, end in windows if start <= c["date"] <= end), None)
        if n is None:
            # Streamed early (e.g. Netflix ONA weeks before the TV broadcast dates).
            n = next((n for n, start, end in windows
                      if c["date"] < start <= c["date"] + EARLY_RELEASE), None)
        if n is not None:
            parts.setdefault(n, []).append(c)
        elif c["id"] not in side:
            late.append(c)
    # Exact-name entries dated far from every season (source lists the original
    # broadcast date differently): give them to the latest earlier season that
    # nothing else claimed.
    for c in sorted(late, key=lambda c: c["date"]):
        free = [n for n, start, end in windows
                if start <= c["date"] <= end + LATE_LISTING and n not in parts]
        if free and free[-1] == max((n for n, start, _ in windows if start <= c["date"]),
                                    default=None):
            parts[free[-1]] = [c]
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
    """(season, 1-based position) per TVDB, accepting within-season or absolute numbers."""
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


def series_show(imdb: str, tvdb_hint=None) -> dict:
    """TVDB seasons + title variants from TVDB and TMDB."""
    tvdb_id = _tvdb_find(imdb, tvdb_hint)
    if not tvdb_id:
        raise Unresolved("imdb not on TVDB")
    show = _tvdb_show(tvdb_id)
    if not show:
        raise Unresolved("TVDB show missing")
    return dict(show, titles=_dedup_titles(show["titles"] + _tmdb_tv_titles(imdb)))


def resolve_series(imdb: str, season: int, episode: int, catalog: Catalog,
                   series_info, find_by_epnum, episodes_in_order, known=(),
                   tvdb_hint=None):
    """Return (episode_dict, series_info_data) or None if not (yet) available.
    Raises Unresolved when the show can't be mapped live."""
    if not TVDB_KEY:
        raise Unresolved("no TVDB key")
    if season <= 0:
        return None
    show = series_show(imdb, tvdb_hint)
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

    # Walk seasons in order, tracking where each anime3rb part starts on the TVDB
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
