"""Build Kitsu ID → anime3rb series_id mapping.

Reads data/series_list.json, searches Kitsu API for each anime name,
and saves the mapping to data/kitsu_map.json.

Run weekly via GitHub Actions to keep the map updated.
"""
import json
import os
import time
import unicodedata
import re

import requests

KITSU_API = "https://kitsu.io/api/edge/anime"
HEADERS = {"Accept": "application/vnd.api+json"}
RATE_LIMIT_DELAY = 0.15  # seconds between requests


def _normalize(name: str) -> str:
    """Normalize anime name for comparison."""
    name = unicodedata.normalize("NFKD", name)
    name = name.lower().strip()
    # Remove common suffixes/noise
    name = re.sub(r'\s*\(tv\s*(special)?\)$', '', name)
    name = re.sub(r'\s*\(ova\)$', '', name)
    name = re.sub(r'[^\w\s]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def search_kitsu(name: str) -> dict | None:
    """Search Kitsu for an anime by name. Returns best match or None."""
    try:
        r = requests.get(KITSU_API, params={
            "filter[text]": name,
            "page[limit]": 5,
        }, headers=HEADERS, timeout=15)
        if r.status_code == 429:
            time.sleep(2)
            r = requests.get(KITSU_API, params={
                "filter[text]": name,
                "page[limit]": 5,
            }, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json().get("data", [])
        if not data:
            return None

        norm_name = _normalize(name)

        # Try exact match first
        for entry in data:
            attrs = entry.get("attributes", {})
            titles = [
                attrs.get("canonicalTitle", ""),
                attrs.get("slug", "").replace("-", " "),
            ]
            # Add all title variants
            for t in (attrs.get("titles") or {}).values():
                if t:
                    titles.append(t)
            for alias in (attrs.get("abbreviatedTitles") or []):
                if alias:
                    titles.append(alias)

            for t in titles:
                if _normalize(t) == norm_name:
                    return {
                        "kitsu_id": entry["id"],
                        "title": attrs.get("canonicalTitle", ""),
                        "episodes": attrs.get("episodeCount"),
                    }

        # Fallback to first result
        attrs = data[0].get("attributes", {})
        return {
            "kitsu_id": data[0]["id"],
            "title": attrs.get("canonicalTitle", ""),
            "episodes": attrs.get("episodeCount"),
        }
    except Exception as e:
        print(f"  [ERROR] Kitsu search failed for '{name}': {e}")
        return None


def build_map():
    """Build the full Kitsu → anime3rb mapping."""
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    series_path = os.path.join(data_dir, "series_list.json")

    with open(series_path) as f:
        series_list = json.load(f)

    print(f"Building Kitsu map for {len(series_list)} series...")

    # Load existing map to skip already-mapped entries
    map_path = os.path.join(data_dir, "kitsu_map.json")
    existing_map = {}
    if os.path.exists(map_path):
        with open(map_path) as f:
            existing_map = json.load(f)

    # kitsu_map: kitsu_id -> anime3rb_series_id
    kitsu_map = dict(existing_map)
    # reverse lookup: anime3rb_series_id -> kitsu_id
    reverse = {v: k for k, v in kitsu_map.items()}

    matched = 0
    skipped = 0
    failed = 0

    for i, s in enumerate(series_list):
        sid = str(s["series_id"])
        name = s.get("name", "")

        if not name:
            continue

        # Skip if already mapped
        if sid in reverse:
            skipped += 1
            continue

        result = search_kitsu(name)
        if result:
            kid = str(result["kitsu_id"])
            # Avoid duplicate kitsu_id mappings (keep first match)
            if kid not in kitsu_map:
                kitsu_map[kid] = sid
                reverse[sid] = kid
                matched += 1
            else:
                # kitsu_id already mapped to another series
                skipped += 1
        else:
            failed += 1

        if (i + 1) % 100 == 0:
            print(f"  Progress: {i+1}/{len(series_list)} | Matched: {matched} | Failed: {failed}")

        time.sleep(RATE_LIMIT_DELAY)

    print(f"\nDone! Matched: {matched} | Skipped: {skipped} | Failed: {failed}")
    print(f"Total map size: {len(kitsu_map)} entries")

    # Save the map
    with open(map_path, "w") as f:
        json.dump(kitsu_map, f, indent=2)
    print(f"Saved to {map_path}")


if __name__ == "__main__":
    build_map()
