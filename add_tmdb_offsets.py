"""Bake per-season absolute-episode offsets into imdb_map.json using TMDB.

The catalogs users run (Trakt / TMDB based) number seasons and episodes the way
TMDB does, which differs from TVDB (e.g. TMDB Bleach = 2 seasons, TVDB = 17).
So numbering must be derived from TMDB, not TVDB.

season_offsets[S] = number of episodes before season S (season 0 / specials excluded)
=> absolute_episode(S, E) = season_offsets[S] + E

The TVDB-keyed `seasons` map is cleared for every series we successfully resolve
on TMDB, so the runtime resolves purely through TMDB offsets + the ordered
anime3rb parts (single series -> find by number; multiple parts -> concatenate).
Series we can't resolve on TMDB keep their previous TVDB data untouched.

Runtime never calls TMDB; offsets are stable metadata baked in here.
"""
import json
import os
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DATA = Path(__file__).parent / "data"
KEY = os.environ.get("TMDB_API_KEY", "")
BASE = "https://api.themoviedb.org/3"


def _get(path, **params):
    params["api_key"] = KEY
    q = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    url = f"{BASE}{path}?{q}"
    last = None
    for _ in range(4):
        try:
            with urllib.request.urlopen(url, timeout=40) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001 - retry transient errors
            last = e
            time.sleep(0.6)
    raise last


def tmdb_id_for(imdb):
    d = _get(f"/find/{imdb}", external_source="imdb_id")
    tv = d.get("tv_results") or []
    return tv[0]["id"] if tv else None


def offsets_for(tmdb_id):
    d = _get(f"/tv/{tmdb_id}")
    seasons = sorted(
        (s for s in d.get("seasons", []) if (s.get("season_number") or 0) >= 1),
        key=lambda s: s["season_number"],
    )
    offsets = {}
    cum = 0
    for s in seasons:
        offsets[str(s["season_number"])] = cum
        cum += int(s.get("episode_count") or 0)
    return offsets


def resolve(item):
    tt, _ = item
    try:
        tid = tmdb_id_for(tt)
        if not tid:
            return tt, None, None
        return tt, tid, offsets_for(tid)
    except Exception as e:  # noqa: BLE001
        return tt, "ERR", str(e)


def main():
    if not KEY:
        print("TMDB_API_KEY not set — skipping.")
        return
    m = json.load(open(DATA / "imdb_map.json"))
    series = m["series"]
    items = list(series.items())
    print(f"resolving TMDB numbering for {len(items)} series ...")

    ok = miss = err = 0
    done = 0
    with ThreadPoolExecutor(max_workers=16) as ex:
        for tt, tid, offs in ex.map(resolve, items):
            done += 1
            if tid == "ERR":
                err += 1
            elif tid is None:
                miss += 1
            else:
                v = series[tt]
                v["tmdb"] = tid
                v["season_offsets"] = offs
                v["seasons"] = {}  # drop TVDB-keyed season map; TMDB offsets are authoritative
                ok += 1
            if done % 100 == 0:
                print(f"  {done}/{len(items)}  ok={ok} miss={miss} err={err}")

    json.dump(m, open(DATA / "imdb_map.json", "w"), ensure_ascii=False)
    print(f"done. tmdb-resolved={ok} no_tmdb={miss} errors={err}")


if __name__ == "__main__":
    main()
