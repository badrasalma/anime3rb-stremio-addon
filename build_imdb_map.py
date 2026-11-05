"""Build IMDB -> anime3rb mapping.

Relations backbone: Fribb/anime-lists (imdb <-> tvdb <-> kitsu <-> mal <-> anidb),
one AniDB entry per cour/season (same granularity as anime3rb).
Titles: manami-project anime-offline-database (title + synonyms), joined by mal/anilist/kitsu/anidb id.
anime3rb series: live `get_series` list (romaji names).

Linkage strategy per entry (TV/ONA): match to an anime3rb series by
  1. normalized-title match (title + synonyms) against anime3rb names  [primary, high quality]
  2. kitsu_map bridge (kitsu_id -> anime3rb id)                        [fallback]

Output: data/imdb_map.json
  { "series": { imdb: {tvdb, seasons:{s:[a3...]}, ordered:[a3...]} },
    "movies": { imdb: {id, type} } }
"""
import json
import os
import re
import unicodedata
import urllib.request
from pathlib import Path

from curl_cffi import requests as cffi_requests

ROOT = Path(__file__).parent
DATA = ROOT / "data"

ANIME_LISTS_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
MANAMI_URL = ("https://github.com/manami-project/anime-offline-database/"
              "releases/download/latest/anime-offline-database-minified.json")
BASE_URL = os.environ.get("ANIME3RB_URL", "https://anime3rb.vip")
USERNAME = os.environ["ANIME3RB_USER"]
PASSWORD = os.environ["ANIME3RB_PASS"]


def _download_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)


def fetch_anime3rb_series():
    url = f"{BASE_URL}/player_api.php?username={USERNAME}&password={PASSWORD}&action=get_series"
    return cffi_requests.get(url, impersonate="chrome", timeout=90).json()


def fetch_anime3rb_vod():
    url = f"{BASE_URL}/player_api.php?username={USERNAME}&password={PASSWORD}&action=get_vod_streams"
    return cffi_requests.get(url, impersonate="chrome", timeout=90).json()


def build_manami_index():
    d = _download_json(MANAMI_URL)
    data = d["data"] if isinstance(d, dict) else d
    idx = {}
    pats = {
        "mal": re.compile(r"myanimelist\.net/anime/(\d+)"),
        "anilist": re.compile(r"anilist\.co/anime/(\d+)"),
        "kitsu": re.compile(r"kitsu\.[a-z]+/anime/(\d+)"),
        "anidb": re.compile(r"anidb\.net/anime/(\d+)"),
    }
    for e in data:
        titles = [e.get("title")] + (e.get("synonyms") or [])
        found = {}
        for s in e.get("sources", []):
            for kind, pat in pats.items():
                m = pat.search(s)
                if m:
                    found[kind] = int(m.group(1))
        for kind, v in found.items():
            idx.setdefault((kind, v), titles)
    return idx


def norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = s.replace("&", " and ")
    s = s.replace("×", " x ")
    # unify season notation
    s = re.sub(r"\b(\d+)(st|nd|rd|th)\s+season\b", r"season \1", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def imdb_list(entry):
    v = entry.get("imdb_id")
    if not v:
        return []
    return v if isinstance(v, list) else [v]


def main():
    print("downloading anime-lists ...")
    anime_lists = _download_json(ANIME_LISTS_URL)
    kitsu_map = json.load(open(DATA / "kitsu_map.json"))
    print("downloading manami database ...")
    manami = build_manami_index()
    print("fetching anime3rb series ...")
    a3_series = fetch_anime3rb_series()
    print("fetching anime3rb VOD (movies) ...")
    a3_vod = fetch_anime3rb_vod()
    print(f"anime-lists={len(anime_lists)} manami_idx={len(manami)} "
          f"a3_series={len(a3_series)} a3_vod={len(a3_vod)}")

    def usable(n: str) -> bool:
        # reject junk/ambiguous normalized titles to avoid false name matches
        if len(n) < 4:
            return False
        if n.isdigit():
            return False
        return True

    # anime3rb normalized-name index
    a3_by_name: dict = {}
    for s in a3_series:
        n = norm(s.get("name") or "")
        if usable(n) and n not in a3_by_name:
            a3_by_name[n] = str(s.get("series_id"))
    a3_ids = {str(s.get("series_id")) for s in a3_series}

    # anime3rb VOD (movies) normalized-name index -> stream_id
    vod_by_name: dict = {}
    for v in a3_vod:
        n = norm(v.get("name") or "")
        if usable(n) and n not in vod_by_name:
            vod_by_name[n] = str(v.get("stream_id"))

    def match_vod(entry):
        for t in titles_for(entry):
            n = norm(t)
            if usable(n) and n in vod_by_name:
                return vod_by_name[n]
        return None

    def titles_for(entry):
        for kind in ("mal", "anilist", "kitsu", "anidb"):
            key = {"mal": "mal_id", "anilist": "anilist_id",
                   "kitsu": "kitsu_id", "anidb": "anidb_id"}[kind]
            v = entry.get(key)
            if v and (kind, v) in manami:
                return manami[(kind, v)]
        return []

    def match_a3(entry):
        # 1. name match (main title + synonyms), guarded against junk
        for t in titles_for(entry):
            n = norm(t)
            if not usable(n):
                continue
            hit = a3_by_name.get(n)
            if hit:
                return hit, "name"
        # 2. kitsu bridge
        k = entry.get("kitsu_id")
        m = kitsu_map.get(str(k)) if k else None
        if m and str(m.get("id")) in a3_ids:
            return str(m.get("id")), "kitsu"
        return None, None

    series_out: dict = {}
    movies_out: dict = {}
    stats = {"name": 0, "kitsu": 0, "none": 0}

    for e in anime_lists:
        imdbs = imdb_list(e)
        if not imdbs:
            continue
        etype = (e.get("type") or "").upper()
        tvdb = e.get("tvdb_id")
        season = (e.get("season") or {}).get("tvdb")
        anidb = e.get("anidb_id") or 0

        a3_id, how = match_a3(e)
        stats[how or "none"] += 1

        for imdb in imdbs:
            if etype == "MOVIE":
                if imdb not in movies_out:
                    vod_id = match_vod(e)
                    if vod_id:
                        movies_out[imdb] = {"id": vod_id, "type": "vod"}
                continue
            # Only real series episodes participate in season/episode numbering.
            # Specials (season 0) and OVA/SPECIAL/MUSIC entries are excluded so they
            # don't pollute the ordered/absolute episode flow.
            if season == 0 or etype not in ("TV", "ONA"):
                continue
            rec = series_out.setdefault(imdb, {"tvdb": tvdb, "_entries": []})
            if tvdb and not rec.get("tvdb"):
                rec["tvdb"] = tvdb
            rec["_entries"].append({"a3": a3_id, "season": season, "anidb": anidb})

    final_series = {}
    for imdb, rec in series_out.items():
        entries = sorted(
            rec["_entries"],
            key=lambda x: ((x["season"] if x["season"] is not None else 0), x["anidb"]),
        )
        ordered = []
        seasons: dict = {}
        for en in entries:
            if not en["a3"]:
                continue
            if en["a3"] not in ordered:
                ordered.append(en["a3"])
            if en["season"] is not None:
                seasons.setdefault(str(en["season"]), [])
                if en["a3"] not in seasons[str(en["season"])]:
                    seasons[str(en["season"])].append(en["a3"])
        if not ordered:
            continue
        final_series[imdb] = {"tvdb": rec.get("tvdb"), "seasons": seasons, "ordered": ordered}

    # Preserve previously baked TVDB season offsets (kept even if add_tvdb_offsets
    # can't run for lack of a TVDB key) so long-series numbering keeps working.
    old_path = DATA / "imdb_map.json"
    if old_path.exists():
        try:
            old = json.load(open(old_path)).get("series", {})
        except (json.JSONDecodeError, OSError):
            old = {}
        for imdb, v in final_series.items():
            offs = old.get(imdb, {}).get("season_offsets")
            if offs and v.get("ordered") == old[imdb].get("ordered"):
                v["season_offsets"] = offs

    out = {"series": final_series, "movies": movies_out}
    json.dump(out, open(DATA / "imdb_map.json", "w"), ensure_ascii=False)
    print(f"linkage stats (per anime-lists entry): {stats}")
    print(f"series imdb entries: {len(final_series)}")
    print(f"movie imdb entries: {len(movies_out)}")
    print(f"series w/ multiple anime3rb parts: {sum(1 for v in final_series.values() if len(v['ordered'])>1)}")


if __name__ == "__main__":
    main()
