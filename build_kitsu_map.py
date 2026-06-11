"""Build Kitsu ID → anime3rb mapping (series + movies).

Reads data/series_list.json and data/vod_list.json,
searches Kitsu API for each anime name,
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
    """Build the full Kitsu → anime3rb mapping (series + movies)."""
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    series_path = os.path.join(data_dir, "series_list.json")

    with open(series_path) as f:
        series_list = json.load(f)

    print(f"Building Kitsu map for {len(series_list)} series...")

    # Load existing map to skip already-mapped entries
    # Map format: kitsu_id -> {"id": "...", "type": "series"|"movie"}
    # Legacy format (string value) is auto-migrated to new format
    map_path = os.path.join(data_dir, "kitsu_map.json")
    existing_map = {}
    if os.path.exists(map_path):
        with open(map_path) as f:
            existing_map = json.load(f)

    # Migrate legacy format (plain string values → dict with type)
    kitsu_map = {}
    for k, v in existing_map.items():
        if isinstance(v, str):
            kitsu_map[k] = {"id": v, "type": "series"}
        else:
            kitsu_map[k] = v

    # reverse lookup: "type:anime3rb_id" -> kitsu_id
    reverse = {}
    for kid, info in kitsu_map.items():
        rkey = f"{info['type']}:{info['id']}"
        reverse[rkey] = kid

    matched = 0
    skipped = 0
    failed = 0

    # --- Series ---
    for i, s in enumerate(series_list):
        sid = str(s["series_id"])
        name = s.get("name", "")

        if not name:
            continue

        if f"series:{sid}" in reverse:
            skipped += 1
            continue

        result = search_kitsu(name)
        if result:
            kid = str(result["kitsu_id"])
            if kid not in kitsu_map:
                kitsu_map[kid] = {"id": sid, "type": "series"}
                reverse[f"series:{sid}"] = kid
                matched += 1
            else:
                skipped += 1
        else:
            failed += 1

        if (i + 1) % 100 == 0:
            print(f"  Series: {i+1}/{len(series_list)} | Matched: {matched} | Failed: {failed}")

        time.sleep(RATE_LIMIT_DELAY)

    print(f"\nSeries done! Matched: {matched} | Skipped: {skipped} | Failed: {failed}")

    # --- Movies (VOD) ---
    vod_path = os.path.join(data_dir, "vod_list.json")
    if os.path.exists(vod_path):
        with open(vod_path) as f:
            vod_list = json.load(f)

        print(f"\nBuilding Kitsu map for {len(vod_list)} movies...")
        m_matched = 0
        m_skipped = 0
        m_failed = 0

        for i, v in enumerate(vod_list):
            vid = str(v.get("stream_id", ""))
            name = v.get("name", "")

            if not name or not vid:
                continue

            if f"movie:{vid}" in reverse:
                m_skipped += 1
                continue

            result = search_kitsu(name)
            if result:
                kid = str(result["kitsu_id"])
                if kid not in kitsu_map:
                    kitsu_map[kid] = {"id": vid, "type": "movie"}
                    reverse[f"movie:{vid}"] = kid
                    m_matched += 1
                else:
                    m_skipped += 1
            else:
                m_failed += 1

            if (i + 1) % 100 == 0:
                print(f"  Movies: {i+1}/{len(vod_list)} | Matched: {m_matched} | Failed: {m_failed}")

            time.sleep(RATE_LIMIT_DELAY)

        print(f"\nMovies done! Matched: {m_matched} | Skipped: {m_skipped} | Failed: {m_failed}")

    print(f"Total map size: {len(kitsu_map)} entries")

    # Save the map
    with open(map_path, "w") as f:
        json.dump(kitsu_map, f, indent=2)
    print(f"Saved to {map_path}")


if __name__ == "__main__":
    build_map()
