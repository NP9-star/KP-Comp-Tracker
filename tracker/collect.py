"""Run every 30 minutes: check each club's public availability and update block state.

State per club (data/state/<key>.json):
  runs:  epoch seconds of successful checks (used to confirm coverage)
  days:  {date: {court: {"HH:MM": {"f": first seen free, "s": when it became taken,
                                   "l": 1 if free at the latest check, "r": price per block}}}}
A block with no entry has never been seen free.
"""
from __future__ import annotations

import sys
import traceback
from datetime import timedelta

from . import matchi, playtomic
from .common import (DATA, blocks_for_day, block_start, hours_by_weekday, load_config,
                     now_utc, read_json, tz, write_json)

ADAPTERS = {"playtomic": playtomic, "matchi": matchi}


def club_hours(club, info):
    return hours_by_weekday(club.get("hours") or info.get("hours") or {"all": "07:00-22:00"})


def update_day(day_state: dict, courts: list[str], blocks: list[str], free: dict, d, zone, now, ts):
    for court in courts:
        cs = day_state.setdefault(court, {})
        fc = free.get(court, {})
        for hm in blocks:
            if block_start(d, hm, zone) <= now:
                continue  # already started: frozen
            e = cs.get(hm)
            if hm in fc:
                if e is None:
                    e = cs[hm] = {"f": ts}
                e["l"] = 1
                e.pop("s", None)  # reappeared (e.g. cancellation)
                if fc[hm] is not None:
                    e["r"] = round(fc[hm], 2)
            elif e is not None and e.get("l") == 1:
                e["l"] = 0
                e["s"] = ts
        if not cs:
            day_state.pop(court, None)


def run():
    cfg = load_config()
    zone = tz(cfg)
    block = int(cfg.get("block_minutes", 30))
    horizon = int(cfg.get("horizon_days", 14))
    resolved = read_json(DATA / "resolved.json", {})
    status = read_json(DATA / "status.json", {})
    now = now_utc()
    ts = int(now.timestamp())
    today = now.astimezone(zone).date()
    failures = 0

    for club in cfg["clubs"]:
        key = club["key"]
        st = status.setdefault(key, {})
        st["last_attempt"] = now.isoformat(timespec="seconds")
        try:
            ad = ADAPTERS[club["platform"]]
            info = ad.resolve(club, resolved)
            hours = club_hours(club, info)
            state = read_json(DATA / "state" / f"{key}.json", {"runs": [], "days": {}})
            for i in range(horizon):
                d = today + timedelta(days=i)
                free = ad.free_blocks(club, info, d, zone, block)
                courts = sorted(info["courts"].values())
                blocks = blocks_for_day(hours, d, block)
                update_day(state["days"].setdefault(d.isoformat(), {}), courts, blocks, free, d, zone, now, ts)
            state["runs"] = [r for r in state["runs"] if r > ts - 40 * 86400] + [ts]
            state["courts"] = sorted(info["courts"].values())
            write_json(DATA / "state" / f"{key}.json", state)
            st.update(last_ok=now.isoformat(timespec="seconds"), error=None,
                      courts=len(info["courts"]))
            print(f"OK   {club['name']}: {len(info['courts'])} courts")
        except Exception as e:  # keep going for the other clubs
            failures += 1
            st["error"] = f"{type(e).__name__}: {e}"[:500]
            print(f"FAIL {club['name']}: {e}", file=sys.stderr)
            traceback.print_exc()

    write_json(DATA / "resolved.json", resolved, compact=False)
    write_json(DATA / "status.json", status, compact=False)
    return failures


if __name__ == "__main__":
    f = run()
    sys.exit(1 if f == len(load_config()["clubs"]) else 0)
