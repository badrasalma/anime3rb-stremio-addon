"""Fetch all series data from anime3rb and save as cached JSON files.

Uses a single cloudscraper session with smart retry and rate limiting.
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
    # Warm up: solve Cloudflare challenge first
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


def main():
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(data_dir, exist_ok=True)

    print("Initializing scraper...")
    scraper = make_scraper()

    # 1. Fetch series list (if not exists)
    sl_path = os.path.join(data_dir, "series_list.json")
    if not os.path.exists(sl_path):
        print("Fetching series list...")
        series_list = api_get(scraper, "get_series", timeout=60)
        print(f"  Got {len(series_list)} series")
        series_slim = [{
            "series_id": s.get("series_id"), "name": s.get("name", ""),
            "cover": s.get("cover", ""), "plot": (s.get("plot", "") or "")[:500],
            "genre": s.get("genre", ""), "category_id": s.get("category_id"),
            "rating": s.get("rating", ""), "releaseDate": s.get("releaseDate", ""),
        } for s in series_list]
        with open(sl_path, "w") as f:
            json.dump(series_slim, f, ensure_ascii=False, separators=(",", ":"))
    else:
        with open(sl_path) as f:
            series_slim = json.load(f)
        print(f"Loaded {len(series_slim)} series from cache")

    # 2. Categories
    cat_path = os.path.join(data_dir, "categories.json")
    if not os.path.exists(cat_path):
        print("Fetching categories...")
        cats = api_get(scraper, "get_series_categories")
        with open(cat_path, "w") as f:
            json.dump(cats, f, ensure_ascii=False, separators=(",", ":"))

    # 3. VOD
    vod_path = os.path.join(data_dir, "vod_list.json")
    if not os.path.exists(vod_path):
        print("Fetching VOD list...")
        vod_list = api_get(scraper, "get_vod_streams", timeout=60)
        vod_slim = [{
            "stream_id": v.get("stream_id"), "name": v.get("name", ""),
            "cover": v.get("cover", ""), "stream_icon": v.get("stream_icon", ""),
            "plot": (v.get("plot", "") or "")[:500], "genre": v.get("genre", ""),
            "category_id": v.get("category_id"), "rating": v.get("rating", ""),
            "releaseDate": v.get("releaseDate", ""),
            "container_extension": v.get("container_extension", "mp4"),
        } for v in vod_list]
        with open(vod_path, "w") as f:
            json.dump(vod_slim, f, ensure_ascii=False, separators=(",", ":"))

    # 4. Episode data - fetch with smart retry
    episodes_path = os.path.join(data_dir, "episodes.json")
    all_info = {}
    if os.path.exists(episodes_path):
        with open(episodes_path) as f:
            all_info = json.load(f)

    series_ids = [str(s["series_id"]) for s in series_slim]
    remaining = [sid for sid in series_ids if sid not in all_info]
    print(f"Episodes: {len(all_info)} cached, {len(remaining)} remaining")

    if not remaining:
        print("All episodes cached!")
        return

    consecutive_fails = 0
    done = 0

    for sid in remaining:
        try:
            data = api_get(scraper, "get_series_info", f"&series_id={sid}")
            if data and "episodes" in data:
                info = data.get("info", {})
                episodes = {}
                for season, eps in data["episodes"].items():
                    episodes[season] = [{
                        "e": ep.get("episode_num"),
                        "s": ep.get("stream_id"),
                        "x": ep.get("container_extension", "mp4"),
                        "t": ep.get("title", ""),
                    } for ep in eps]
                all_info[sid] = {
                    "name": info.get("name", ""),
                    "cover": info.get("cover", ""),
                    "plot": (info.get("plot", "") or "")[:500],
                    "genre": info.get("genre", ""),
                    "rating": info.get("rating", ""),
                    "releaseDate": info.get("releaseDate", ""),
                    "episodes": episodes,
                }
                consecutive_fails = 0
            else:
                consecutive_fails += 1
        except Exception as e:
            consecutive_fails += 1
            if consecutive_fails >= 5:
                print(f"  {len(all_info)} cached. Re-initializing scraper after {consecutive_fails} fails...")
                # Save progress
                with open(episodes_path, "w") as f:
                    json.dump(all_info, f, ensure_ascii=False, separators=(",", ":"))
                time.sleep(30)
                scraper = make_scraper()
                consecutive_fails = 0
                time.sleep(5)

        done += 1
        if done % 25 == 0:
            print(f"  Progress: {done}/{len(remaining)} (total cached: {len(all_info)})")
            with open(episodes_path, "w") as f:
                json.dump(all_info, f, ensure_ascii=False, separators=(",", ":"))

        # Rate limiting: 1 request every 2 seconds
        time.sleep(2)

    # Final save
    with open(episodes_path, "w") as f:
        json.dump(all_info, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Done! Total cached: {len(all_info)} series")


if __name__ == "__main__":
    main()
