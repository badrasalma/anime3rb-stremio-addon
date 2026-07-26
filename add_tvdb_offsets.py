"""Bake per-season absolute-episode offsets into imdb_map.json.

Only single-long-series (one anime3rb series, but IMDB/TVDB split into many seasons)
need this: to convert an incoming (season, episode) into an absolute episode number
that matches anime3rb's continuous numbering.

season_offsets[S] = number of aired episodes before season S (aired order, season 0 excluded)
=> absolute_episode(S, E) = season_offsets[S] + E

Runtime never calls TVDB; offsets are stable metadata. New episodes within an
existing season resolve fine (offset + E). A brand-new season needs a map rebuild.
"""
import json
import os
import time
import urllib.request
from pathlib import Path

DATA = Path(__file__).parent / "data"
TVDB_KEY = os.environ.get("TVDB_API_KEY", "")


def login():
    req = urllib.request.Request(
        "https://api4.thetvdb.com/v4/login",
        data=json.dumps({"apikey": TVDB_KEY}).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=30))["data"]["token"]


def get(url, tok):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
    return json.load(urllib.request.urlopen(req, timeout=40))


def season_offsets(tvdb_id, tok):
    eps = []
    for pg in range(0, 12):
        d = get(f"https://api4.thetvdb.com/v4/series/{tvdb_id}/episodes/default?page={pg}", tok)
        page = d["data"]["episodes"]
        eps += page
        if len(page) < 500:
            break
    # aired order, exclude specials (season 0)
    reg = [e for e in eps if (e.get("seasonNumber") or 0) > 0]
    reg.sort(key=lambda e: (e.get("seasonNumber"), e.get("number") or 0))
    # offset[S] = absolute number of first episode of S  -  its within-season number
    # => absolute(S, E) = offset[S] + E   (uses TVDB's own absoluteNumber, gap-safe)
    offsets = {}
    for e in reg:
        s = str(e.get("seasonNumber"))
        if s in offsets:
            continue
        absn = e.get("absoluteNumber")
        num = e.get("number")
        if absn and num:
            offsets[s] = absn - num
    return offsets


def needs_tvdb(v):
    seasons = v["seasons"]
    ordered = v["ordered"]
    if not seasons:
        return True
    mapped = set()
    for arr in seasons.values():
        mapped.update(arr)
    return set(ordered) != mapped


def main():
    if not TVDB_KEY:
        print("TVDB_API_KEY not set — skipping offset baking "
              "(existing offsets are preserved by build_imdb_map.py).")
        return
    m = json.load(open(DATA / "imdb_map.json"))
    series = m["series"]
    targets = [(tt, v) for tt, v in series.items() if needs_tvdb(v) and v.get("tvdb")]
    print(f"computing offsets for {len(targets)} series")
    tok = login()
    ok = 0
    for i, (tt, v) in enumerate(targets, 1):
        try:
            offs = season_offsets(v["tvdb"], tok)
            if offs:
                v["season_offsets"] = offs
                ok += 1
        except Exception as e:
            print(f"  {tt} tvdb={v['tvdb']} FAILED: {e}")
        if i % 10 == 0:
            print(f"  {i}/{len(targets)}")
        time.sleep(0.15)
    json.dump(m, open(DATA / "imdb_map.json", "w"), ensure_ascii=False)
    print(f"done. offsets added to {ok} series")


if __name__ == "__main__":
    main()
