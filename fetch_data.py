"""Fetch series data from anime3rb and save as cached JSON files.

Modes:
  full   - Fetch everything (initial setup, slow)
  update - Quick daily update: add new series + refresh recently added ones
           New episodes for existing series are handled by live API fallback
           in main.py, so this only needs to keep the cache reasonably fresh.
"""
import json
import os
import sys
import time

import cloudscraper

BASE_URL = os.environ.get("ANIME3RB_URL", "https://anime3rb.vip")
USERNAME = os.environ.get("ANIME3RB_USER", "bbnmnbb")
PASSWORD = os.environ.get("ANIME3RB_PASS", "as209509")


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


def _fetch_series_batch(scraper, all_info, series_ids, data_dir):
    """Fetch episode data for a list of series IDs sequentially."""
    episodes_path = os.path.join(data_dir, "episodes.json")
    consecutive_fails = 0
    done = 0
    updated = 0
    failed = 0

    for sid in series_ids:
        success = False
        for retry in range(3):
            try:
                data = api_get(scraper, "get_series_info", f"&series_id={sid}")
                if data and "episodes" in data:
                    parsed = _parse_episodes(data)
                    new_count = _ep_count(parsed)
                    old_count = _ep_count(all_info.get(sid, {}))
                    if new_count != old_count or sid not in all_info:
                        all_info[sid] = parsed
                        updated += 1
                    consecutive_fails = 0
                    success = True
                    break
                else:
                    consecutive_fails += 1
            except Exception:
                consecutive_fails += 1

            if consecutive_fails >= 5:
                print(f"  Re-initializing scraper after {consecutive_fails} fails...")
                with open(episodes_path, "w") as f:
                    json.dump(all_info, f, ensure_ascii=False, separators=(",", ":"))
                time.sleep(30)
                scraper = make_scraper()
                consecutive_fails = 0
                time.sleep(5)
            elif retry < 2:
                time.sleep(3)

        if not success:
            failed += 1

        done += 1
        if done % 50 == 0:
            print(f"  Progress: {done}/{len(series_ids)} (updated: {updated}, failed: {failed})")
            with open(episodes_path, "w") as f:
                json.dump(all_info, f, ensure_ascii=False, separators=(",", ":"))

        time.sleep(1)

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
    print(f"  Got {len(series_slim)} series from API")
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
        # Quick update: only fetch NEW series not in our cache
        # Live API fallback in main.py handles new episodes for existing series
        new_ids = [sid for sid in all_ids if sid not in all_info]
        print(f"Cache has {len(all_info)} series. Server has {len(all_ids)}. New: {len(new_ids)}")

        if not new_ids:
            print("No new series to fetch — cache is up to date!")
        else:
            print(f"Fetching {len(new_ids)} new series...")
            updated, failed = _fetch_series_batch(scraper, all_info, new_ids, data_dir)
            print(f"Done! Added {updated} new series, failed {failed} (total cached: {len(all_info)})")
    else:
        # Full mode: fetch all uncached series
        remaining = [sid for sid in all_ids if sid not in all_info]
        print(f"Episodes: {len(all_info)} cached, {len(remaining)} remaining")
        if not remaining:
            print("All episodes cached!")
            return
        updated, failed = _fetch_series_batch(scraper, all_info, remaining, data_dir)
        print(f"Done! Total cached: {len(all_info)} series, failed: {failed}")


if __name__ == "__main__":
    main()
