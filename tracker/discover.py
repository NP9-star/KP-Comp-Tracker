"""One-off check that each club can be reached and parsed. Run from the Actions tab
("Run workflow" -> mode: discover) or locally with:  python -m tracker.discover
Copy the log output back to Claude if anything looks wrong."""
from __future__ import annotations

import collections
import json
from datetime import timedelta

from . import matchi, playtomic
from .common import DATA, load_config, local_day_bounds_utc, now_utc, tz, write_json


def main():
    cfg = load_config()
    zone = tz(cfg)
    day = now_utc().astimezone(zone).date() + timedelta(days=1)
    resolved: dict = {}
    dbg = DATA / "debug"
    dbg.mkdir(parents=True, exist_ok=True)
    for club in cfg["clubs"]:
        print("=" * 70)
        print(club["name"], f"({club['platform']})")
        try:
            if club["platform"] == "playtomic":
                info = playtomic.resolve(club, resolved)
                print("tenant:", info["tenant_id"], "|", info.get("tenant_name"))
                print("courts:", list(info["courts"].values()))
                print("hours from Playtomic:", info.get("hours"))
                s, e = local_day_bounds_utc(day, zone)
                raw = playtomic._get("/availability", {"sport_id": "PADEL", "tenant_id": info["tenant_id"],
                                     "start_min": s.strftime("%Y-%m-%dT%H:%M:%S"),
                                     "start_max": e.strftime("%Y-%m-%dT%H:%M:%S")})
                write_json(dbg / f"{club['key']}_availability.json", raw, compact=False)
                starts = sorted({sl["start_time"] for r in raw for sl in r.get("slots", [])})
                print(f"raw slot start times for {day} (first/last):", starts[:3], starts[-3:])
                print("sample slot:", json.dumps((raw[0].get("slots") or [None])[0] if raw else None))
                free = playtomic.free_blocks(club, info, day, zone, cfg["block_minutes"])
                print("free 30-min blocks per court (local time):",
                      {c: len(v) for c, v in free.items()})
                first = min((min(v) for v in free.values() if v), default=None)
                print("earliest free block, local:", first,
                      "  <- should be at or after opening time; if it is 1h early, set times_are_local")
            else:
                info = matchi.resolve(club, resolved)
                print("facility:", info["facility_id"], "| sport id:", info["sport"], "|", info.get("name"))
                html = matchi.schedule_html(info["facility_id"], day, info["sport"], None)
                (dbg / f"{club['key']}_schedule.html").write_text(html)
                cells = list(matchi.parse_cells(html))
                print("cells:", len(cells), "| class counts:",
                      dict(collections.Counter(c[4] for c in cells).most_common(6)))
                print("courts:", sorted({c[0] for c in cells}))
                print("sample cells:", [(c[0], c[1] // 60, c[3]) for c in cells[:4]])
        except Exception as ex:
            print("ERROR:", type(ex).__name__, ex)
    print("=" * 70)
    print("Raw responses saved in data/debug/ (downloadable from the workflow run).")


if __name__ == "__main__":
    main()
