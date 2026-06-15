"""Fetch series data from anime3rb and save as cached JSON files.

Modes:
  full   - Fetch everything (initial setup)
  update - Re-fetch all series using concurrent requests (daily cron)
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import cloudscraper

BASE_URL = os.environ.get("ANIME3RB_URL", "https://anime3rb.vip")
USERNAME = os.environ.get("ANIME3RB_USER", "bbnmnbb")
PASSWORD = os.environ.get("ANIME3RB_PASS", "as209509")

# Shared scraper pool for threads
_scrapers = []


def make_scraper():
    s = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "linux"})
    url = f"{BASE_URL}/player_api.php?username={USERNAME}&password={PASSWORD}"
    for attempt in range(3):
        try:
            r = s.get(url, timeout=30)
            if r.status_code == 200:
                return s
        except Exception:
            pass
        time.sleep(5)
    return s


def api_get(scraper, action, extra="", timeout=30):
    url = f"{BASE_URL}/player_api.php?username={USERNAME}&password={PASSWORD}&action={action}{extra}"
    r = scraper.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _slim_series(raw_list):
    return [{
        "series_id": s.get("series_id"), "name": s.get("name", ""),
        "cover": s.get("cover", ""), "plot": (s.get("plot", "") or "")[:500],
        "genre": s.get("genre", ""), "category_id": s.get("category_id"),
        "rating": s.get("rating", ""), "releaseDate": s.get("releaseDate", ""),
    } for s in raw_list]


def _parse_episodes(data):
    """Convert API response to compact cached format."""
    info = data.get("info", {})
    episodes = {}
    for season, eps in data.get("episodes", {}).items():
        episodes[season] = [{
            "e": ep.get("episode_num"),
            "s": ep.get("stream_id") or ep.get("id"),
            "x": ep.get("container_extension", "mp4"),
            "t": ep.get("title", ""),
        } for ep in eps]
    return {
        "name": info.get("name", ""),
        "cover": info.get("cover", ""),
        "plot": (info.get("plot", "") or "")[:200],
        "genre": info.get("genre", ""),
        "rating": info.get("rating", ""),
        "releaseDate": info.get("releaseDate", ""),
        "episodes": episodes,
    }


def _ep_count(entry):
    return sum(len(eps) for eps in entry.get("episodes", {}).values())


def _fetch_one(sid):
    """Fetch a single series (for use in thread pool)."""
    import threading
    tid = threading.current_thread().ident
    # Each thread gets its own scraper
    if not hasattr(_fetch_one, "_local"):
        _fetch_one._local = threading.local()
    local = _fetch_one._local
    if not hasattr(local, "scraper"):
        local.scraper = make_scraper()
        local.fail_count = 0

    for retry in range(3):
        try:
            data = api_get(local.scraper, "get_series_info", f"&series_id={sid}")
            if data and "episodes" in data:
                local.fail_count = 0
                return sid, _parse_episodes(data)
            local.fail_count += 1
        except Exception:
            local.fail_count += 1

        if local.fail_count >= 5:
            time.sleep(15)
            local.scraper = make_scraper()
            local.fail_count = 0
        elif retry < 2:
            time.sleep(2)

    return sid, None


def _fetch_episodes_concurrent(all_info, series_ids, data_dir, max_workers=8):
    """Fetch episode data for series IDs using concurrent requests."""
    episodes_path = os.path.join(data_dir, "episodes.json")
    done = 0
    updated = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_one, sid): sid for sid in series_ids}

        for future in as_completed(futures):
            sid, parsed = future.result()
            done += 1

            if parsed is not None:
                new_count = _ep_count(parsed)
                old_count = _ep_count(all_info.get(sid, {}))
                if new_count != old_count or sid not in all_info:
                    all_info[sid] = parsed
                    updated += 1
            else:
                failed += 1

            if done % 100 == 0:
                print(f"  Progress: {done}/{len(series_ids)} (updated: {updated}, failed: {failed})")
                with open(episodes_path, "w") as f:
                    json.dump(all_info, f, ensure_ascii=False, separators=(",", ":"))

    # Final save
    with open(episodes_path, "w") as f:
        json.dump(all_info, f, ensure_ascii=False, separators=(",", ":"))

    return updated, failed


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(data_dir, exist_ok=True)

    print(f"Mode: {mode}")
    print("Initializing scraper...")
    scraper = make_scraper()
    print("  Scraper ready")

    sl_path = os.path.join(data_dir, "series_list.json")
    cat_path = os.path.join(data_dir, "categories.json")
    episodes_path = os.path.join(data_dir, "episodes.json")

    # ── Always refresh series list ──
    print("Fetching series list...")
    raw = api_get(scraper, "get_series", timeout=60)
    series_slim = _slim_series(raw)
    print(f"  Got {len(series_slim)} series")
    with open(sl_path, "w") as f:
        json.dump(series_slim, f, ensure_ascii=False, separators=(",", ":"))
    time.sleep(2)

    if mode == "update" or not os.path.exists(cat_path):
        print("Fetching categories...")
        cats = api_get(scraper, "get_series_categories")
        with open(cat_path, "w") as f:
            json.dump(cats, f, ensure_ascii=False, separators=(",", ":"))
        time.sleep(2)

    # ── Episode data ──
    all_info = {}
    if os.path.exists(episodes_path):
        with open(episodes_path) as f:
            all_info = json.load(f)

    all_ids = [str(s["series_id"]) for s in series_slim]

    if mode == "update":
        print(f"Updating all {len(all_ids)} series (8 concurrent workers)...")
        updated, failed = _fetch_episodes_concurrent(all_info, all_ids, data_dir, max_workers=8)
        print(f"Done! Updated {updated} series, failed {failed} (total cached: {len(all_info)})")
    else:
        # Full mode: only fetch uncached series
        remaining = [sid for sid in all_ids if sid not in all_info]
        print(f"Episodes: {len(all_info)} cached, {len(remaining)} remaining")
        if not remaining:
            print("All episodes cached!")
            return
        updated, failed = _fetch_episodes_concurrent(all_info, remaining, data_dir, max_workers=8)
        print(f"Done! Total cached: {len(all_info)} series, failed: {failed}")


if __name__ == "__main__":
    main()
