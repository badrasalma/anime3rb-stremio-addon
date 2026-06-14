"""Build TMDB ID → anime3rb VOD stream_id mapping for movies.

Uses TMDB API to search movie names and find TMDB IDs.
Output: data/tmdb_map.json  {tmdb_id: vod_stream_id}
"""
import json
import os
import time
import requests

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "a482c1a297d1a38b4e6f9d4c9c1eaa35")
TMDB_SEARCH_URL = "https://api.themoviedb.org/3/search/movie"


def search_tmdb(name: str) -> str | None:
    """Search TMDB for a movie name and return TMDB ID as string."""
    try:
        r = requests.get(
            TMDB_SEARCH_URL,
            params={"api_key": TMDB_API_KEY, "query": name},
            timeout=15,
        )
        if r.status_code == 200:
            results = r.json().get("results", [])
            if results:
                # Return first result (most relevant)
                return str(results[0]["id"])
    except Exception:
        pass
    return None


def build_tmdb_map():
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    vod_path = os.path.join(data_dir, "vod_list.json")
    map_path = os.path.join(data_dir, "tmdb_map.json")

    with open(vod_path) as f:
        vod_list = json.load(f)

    # Load existing map
    existing_map = {}
    if os.path.exists(map_path):
        with open(map_path) as f:
            existing_map = json.load(f)

    # Reverse lookup to skip already mapped
    reverse = {v: k for k, v in existing_map.items()}

    print(f"Building TMDB map for {len(vod_list)} movies (existing: {len(existing_map)})...")

    matched = 0
    skipped = 0
    failed = 0
    failed_names = []

    for i, v in enumerate(vod_list):
        vid = str(v.get("stream_id", ""))
        name = v.get("name", "")

        if not name or not vid:
            continue

        if vid in reverse:
            skipped += 1
            continue

        tmdb_id = search_tmdb(name)
        if tmdb_id:
            if tmdb_id not in existing_map:
                existing_map[tmdb_id] = vid
                reverse[vid] = tmdb_id
                matched += 1
            else:
                skipped += 1
        else:
            failed += 1
            failed_names.append(name)

        if (i + 1) % 50 == 0:
            print(f"  Progress: {i+1}/{len(vod_list)} | Matched: {matched} | Failed: {failed} | Skipped: {skipped}")
            with open(map_path, "w") as f:
                json.dump(existing_map, f, ensure_ascii=False, separators=(",", ":"))

        time.sleep(0.25)  # TMDB rate limit: 40 req/10s

    # Final save
    with open(map_path, "w") as f:
        json.dump(existing_map, f, ensure_ascii=False, separators=(",", ":"))

    print(f"\nDone! Total TMDB mappings: {len(existing_map)}")
    print(f"  New matched: {matched}")
    print(f"  Skipped (already mapped): {skipped}")
    print(f"  Failed to match: {failed}")
    if failed_names[:10]:
        print(f"  Sample unmatched: {failed_names[:10]}")


if __name__ == "__main__":
    build_tmdb_map()
