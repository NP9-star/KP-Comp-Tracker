"""Connection check. Run from the Actions tab (Run workflow -> mode: discover) and send the log to Claude."""
from __future__ import annotations

import collections
import json
from datetime import timedelta

from . import matchi, net, playtomic
from .collect import club_hours
from .common import DATA, load_config, min_to_hm, now_utc, tz, write_json


def main():
    cfg = load_config()
    zone = tz(cfg)
    block = int(cfg.get("block_minutes", 30))
    day = now_utc().astimezone(zone).date() + timedelta(days=1)
    resolved: dict = {}
    dbg = DATA / "debug"
    dbg.mkdir(parents=True, exist_ok=True)
    matchi.DEBUG["on"] = True
    pt = []
    for club in cfg["clubs"]:
        print("=" * 70)
        print(club["name"], f"({club['platform']})", "checking", day)
        try:
            if club["platform"] == "playtomic":
                info = playtomic.resolve(club, resolved)
                pt.append((club, info))
                print("tenant:", info["tenant_id"])
                print("club page:", "OK" if not info.get("page_error") else f"FAILED {info['page_error']}")
                print("court names from page:", info.get("page_names") or "none found")
                raw = playtomic.availability(club, info["tenant_id"], day)
                write_json(dbg / f"{club['key']}_availability.json", raw, compact=False)
                starts = sorted({s["start_time"] for r in raw or [] for s in r.get("slots", [])})
                print(f"resources in response: {len(raw or [])}; raw start times first/last:",
                      starts[:3], starts[-3:])
                print("sample:", json.dumps({"start_date": raw[0].get("start_date"),
                                             "slot": (raw[0].get("slots") or [None])[0]}) if raw else None)
            else:
                info = matchi.resolve(club, resolved)
                print("facility id:", info["facility_id"], "| sport id:", info["sport"])
                html = matchi.schedule_html(club, info["facility_id"], day, info["sport"], None)
                cells = list(matchi.parse_cells(html))
                print("cells:", len(cells), "| classes:", dict(collections.Counter(c[4] for c in cells).most_common(5)))
                print("courts:", sorted({c[0] for c in cells}))
                print("sample:", [(c[0], min_to_hm(c[1]), "free" if c[3] else "taken") for c in cells[:4]])
        except Exception as ex:  # noqa: BLE001
            print("ERROR:", type(ex).__name__, str(ex)[:600])
    print("=" * 70)
    try:
        playtomic.detect_times(pt, resolved, zone, club_hours)
        print("Playtomic time format:", resolved.get("_playtomic"))
    except Exception as ex:  # noqa: BLE001
        print("Playtomic time format check failed:", ex)
    for club, info in pt:
        try:
            mode = playtomic.times_mode(club, resolved)
            free = playtomic.free_blocks(club, info, day, zone, block, mode)
            print(f"{club['name']}: courts tracked {sorted(info['courts'].values())}")
            print(f"   excluded {sorted(set(info.get('all_courts', {}).values()) - set(info['courts'].values()))}")
            print("   free blocks per court:", {c: len(v) for c, v in free.items()},
                  "| earliest/latest free (local):",
                  min((min(v) for v in free.values() if v), default=None),
                  max((max(v) for v in free.values() if v), default=None))
        except Exception as ex:  # noqa: BLE001
            print(club["name"], "ERROR:", ex)
    if net.used_browser:
        print("Headless-browser fallback was needed for:", ", ".join(sorted(net.used_browser)))
    net.close()
    print("Raw responses saved in data/debug/ (download 'debug' from the run summary page).")


if __name__ == "__main__":
    main()
