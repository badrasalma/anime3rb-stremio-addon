"""Fetch series data from anime3rb and save as cached JSON files.

Modes:
  full   - Fetch everything (initial setup)
  update - Re-fetch series/vod lists + refresh episodes for all cached series
           that have new episodes on the server (daily cron)
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
                print(f"  Scraper ready (cookies: {len(s.cookies)})")
                return s
        except Exception:
            pass
        time.sleep(5)
    print("  WARNING: Scraper may not have valid cookies")
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


def _slim_vod(raw_list):
    return [{
        "stream_id": v.get("stream_id"), "name": v.get("name", ""),
        "cover": v.get("cover", ""), "stream_icon": v.get("stream_icon", ""),
        "plot": (v.get("plot", "") or "")[:500], "genre": v.get("genre", ""),
        "category_id": v.get("category_id"), "rating": v.get("rating", ""),
        "releaseDate": v.get("releaseDate", ""),
        "container_extension": v.get("container_extension", "mp4"),
    } for v in raw_list]


def _parse_episodes(data):
    """Convert API response to compact cached format."""
    info = data.get("info", {})
    episodes = {}
    for season, eps in data.get("episodes", {}).items():
        episodes[season] = [{
            "e": ep.get("episode_num"),
            "s": ep.get("stream_id"),
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


def _fetch_episodes(scraper, all_info, series_ids, data_dir):
    """Fetch episode data for a list of series IDs."""
    episodes_path = os.path.join(data_dir, "episodes.json")
    consecutive_fails = 0
    done = 0
    updated = 0
    failed_ids = []

    for sid in series_ids:
        success = False
        for retry in range(3):
            try:
                data = api_get(scraper, "get_series_info", f"&series_id={sid}")
                if data and "episodes" in data:
                    parsed = _parse_episodes(data)
                    new_count = _ep_count(parsed)
                    old_count = _ep_count(all_info.get(sid, {}))
                    if new_count != old_count:
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
                time.sleep(5)

        if not success:
            failed_ids.append(sid)

        done += 1
        if done % 25 == 0:
            print(f"  Progress: {done}/{len(series_ids)} (updated: {updated})")
            with open(episodes_path, "w") as f:
                json.dump(all_info, f, ensure_ascii=False, separators=(",", ":"))

        time.sleep(2)

    # Retry failed series one more time
    if failed_ids:
        print(f"  Retrying {len(failed_ids)} failed series...")
        time.sleep(30)
        scraper = make_scraper()
        for sid in failed_ids:
            try:
                data = api_get(scraper, "get_series_info", f"&series_id={sid}")
                if data and "episodes" in data:
                    parsed = _parse_episodes(data)
                    new_count = _ep_count(parsed)
                    old_count = _ep_count(all_info.get(sid, {}))
                    if new_count != old_count:
                        all_info[sid] = parsed
                        updated += 1
            except Exception:
                pass
            time.sleep(3)

    # Final save
    with open(episodes_path, "w") as f:
        json.dump(all_info, f, ensure_ascii=False, separators=(",", ":"))
    return updated


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(data_dir, exist_ok=True)

    print(f"Mode: {mode}")
    print("Initializing scraper...")
    scraper = make_scraper()

    sl_path = os.path.join(data_dir, "series_list.json")
    cat_path = os.path.join(data_dir, "categories.json")
    vod_path = os.path.join(data_dir, "vod_list.json")
    episodes_path = os.path.join(data_dir, "episodes.json")

    # ── Always refresh series list, categories, VOD in update mode ──
    if mode == "update" or not os.path.exists(sl_path):
        print("Fetching series list...")
        raw = api_get(scraper, "get_series", timeout=60)
        series_slim = _slim_series(raw)
        print(f"  Got {len(series_slim)} series")
        with open(sl_path, "w") as f:
            json.dump(series_slim, f, ensure_ascii=False, separators=(",", ":"))
        time.sleep(2)
    else:
        with open(sl_path) as f:
            series_slim = json.load(f)
        print(f"Loaded {len(series_slim)} series from cache")

    if mode == "update" or not os.path.exists(cat_path):
        print("Fetching categories...")
        cats = api_get(scraper, "get_series_categories")
        with open(cat_path, "w") as f:
            json.dump(cats, f, ensure_ascii=False, separators=(",", ":"))
        time.sleep(2)

    if mode == "update" or not os.path.exists(vod_path):
        print("Fetching VOD list...")
        vod_list = api_get(scraper, "get_vod_streams", timeout=60)
        with open(vod_path, "w") as f:
            json.dump(_slim_vod(vod_list), f, ensure_ascii=False, separators=(",", ":"))
        time.sleep(2)

    # ── Episode data ──
    all_info = {}
    if os.path.exists(episodes_path):
        with open(episodes_path) as f:
            all_info = json.load(f)

    all_ids = [str(s["series_id"]) for s in series_slim]

    if mode == "update":
        # Re-fetch ALL series to catch new episodes everywhere
        print(f"Refreshing all {len(all_ids)} series for new episodes...")
        updated = _fetch_episodes(scraper, all_info, all_ids, data_dir)
        print(f"Done! Updated {updated} series with new episodes (total: {len(all_info)})")
    else:
        # Full mode: only fetch uncached series
        remaining = [sid for sid in all_ids if sid not in all_info]
        print(f"Episodes: {len(all_info)} cached, {len(remaining)} remaining")
        if not remaining:
            print("All episodes cached!")
            return
        updated = _fetch_episodes(scraper, all_info, remaining, data_dir)
        print(f"Done! Total cached: {len(all_info)} series")


if __name__ == "__main__":
    main()
