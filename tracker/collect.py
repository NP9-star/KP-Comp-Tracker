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

from . import matchi, net, padelos, playtomic
from .common import (DATA, blocks_for_day, block_start, hours_by_weekday, load_config,
                     min_to_hm, now_utc, read_json, tz, write_json, hm_to_min)

ADAPTERS = {"playtomic": playtomic, "matchi": matchi, "padelos": padelos}
DEFAULT_HOURS = {"all": "07:00-22:00"}


def club_hours(club, info):
    """Configured hours, else hours read from the platform, else the range of slots seen so far."""
    if club.get("hours"):
        return hours_by_weekday(club["hours"])
    if info.get("hours"):
        return hours_by_weekday(info["hours"])
    seen = info.get("seen_range")
    if seen:
        return hours_by_weekday({"all": f"{min_to_hm(seen[0])}-{min_to_hm(seen[1]) if seen[1] < 1440 else '24:00'}"})
    return hours_by_weekday(DEFAULT_HOURS)


def note_seen_range(info, free, block):
    times = [hm_to_min(hm) for c in free.values() for hm in c]
    if not times:
        return
    lo, hi = min(times), max(times) + block
    cur = info.get("seen_range") or [lo, hi]
    info["seen_range"] = [min(cur[0], lo), max(cur[1], hi)]


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

    # resolve every club first (so Playtomic's time format can be checked once)
    ready = []
    for club in cfg["clubs"]:
        st = status.setdefault(club["key"], {})
        st["last_attempt"] = now.isoformat(timespec="seconds")
        try:
            ready.append((club, ADAPTERS[club["platform"]].resolve(club, resolved)))
        except Exception as e:  # noqa: BLE001
            failures += 1
            st["error"] = f"{type(e).__name__}: {e}"[:500]
            print(f"FAIL {club['name']}: {e}", file=sys.stderr)
    try:
        playtomic.detect_times([(c, i) for c, i in ready if c["platform"] == "playtomic"],
                               resolved, zone, club_hours)
    except Exception as e:  # noqa: BLE001
        print(f"WARN could not check Playtomic time format: {e}", file=sys.stderr)

    for club, info in ready:
        key, st = club["key"], status[club["key"]]
        try:
            ad = ADAPTERS[club["platform"]]
            mode = playtomic.times_mode(club, resolved) if club["platform"] == "playtomic" else None
            frees = {}
            for i in range(horizon):
                d = today + timedelta(days=i)
                frees[d] = ad.free_blocks(club, info, d, zone, block, mode)
                note_seen_range(info, frees[d], block)
            hours = club_hours(club, info)
            state = read_json(DATA / "state" / f"{key}.json", {"runs": [], "days": {}})
            courts = sorted(info["courts"].values())
            for d, free in frees.items():
                update_day(state["days"].setdefault(d.isoformat(), {}), courts,
                           blocks_for_day(hours, d, block), free, d, zone, now, ts)
            state["runs"] = [r for r in state["runs"] if r > ts - 40 * 86400] + [ts]
            state["courts"] = courts
            write_json(DATA / "state" / f"{key}.json", state)
            st.update(last_ok=now.isoformat(timespec="seconds"), error=None, courts=len(courts))
            print(f"OK   {club['name']}: {len(courts)} courts")
        except Exception as e:  # noqa: BLE001
            failures += 1
            st["error"] = f"{type(e).__name__}: {e}"[:500]
            print(f"FAIL {club['name']}: {e}", file=sys.stderr)
            traceback.print_exc()

    if net.used_browser:
        print("Note: headless-browser fallback was needed for:", ", ".join(sorted(net.used_browser)))
    write_json(DATA / "resolved.json", resolved, compact=False)
    write_json(DATA / "status.json", status, compact=False)
    net.close()
    return failures


if __name__ == "__main__":
    f = run()
    sys.exit(1 if f >= len(load_config()["clubs"]) else 0)
