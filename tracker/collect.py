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
    """Configured hours, else hours read from the club page, else the range of slots seen so far.
    Widened on any weekday where the club has actually offered slots outside those hours."""
    if club.get("hours"):
        hours = hours_by_weekday(club["hours"])
    elif info.get("hours"):
        hours = hours_by_weekday(info["hours"])
    elif info.get("seen_range"):
        seen = info["seen_range"]
        hours = hours_by_weekday({"all": f"{min_to_hm(seen[0])}-{min_to_hm(seen[1]) if seen[1] < 1440 else '24:00'}"})
    else:
        hours = hours_by_weekday(DEFAULT_HOURS)
    for dow, (lo, hi) in (info.get("seen_by_dow") or {}).items():
        dow = int(dow)
        if dow in hours:
            hours[dow] = (min(hours[dow][0], lo), max(hours[dow][1], hi))
    return hours


def court_kinds(club, info):
    """{court name: indoor/covered/outdoor/unknown}"""
    out = {}
    for rid, name in (info.get("courts") or {}).items():
        if club["platform"] == "playtomic":
            out[name] = playtomic.court_kind(club, info, rid)
        else:
            out[name] = playtomic.normalise_kind((info.get("court_kinds") or {}).get(name)
                                                 or club.get("court_type", ""))
    return out


def note_seen_range(info, free, block, d=None):
    times = [hm_to_min(hm) for c in free.values() for hm in c]
    if not times:
        return
    lo, hi = min(times), max(times) + block
    cur = info.get("seen_range") or [lo, hi]
    info["seen_range"] = [min(cur[0], lo), max(cur[1], hi)]
    if d is not None:
        by = info.setdefault("seen_by_dow", {})
        c = by.get(str(d.weekday())) or [lo, hi]
        by[str(d.weekday())] = [min(c[0], lo), max(c[1], hi)]


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
            near = int(cfg.get("near_days", 3))
            far_every = max(1, int(cfg.get("far_days_every_n_runs", 4)))
            run_no = ts // 1800
            for i in range(horizon):
                if i >= near and run_no % far_every:
                    continue  # days further ahead are checked less often, to keep requests low
                d = today + timedelta(days=i)
                frees[d] = ad.free_blocks(club, info, d, zone, block, mode)
                note_seen_range(info, frees[d], block, d)
            if club["platform"] == "playtomic":
                playtomic.auto_exclude_singles(club, info)
                info["courts"] = {r: n for r, n in (info.get("all_courts") or info["courts"]).items()
                                  if not playtomic._excluded(club, r, n) and r not in (info.get("auto_excluded") or [])}
                for d in frees:
                    for r in info.get("auto_excluded") or []:
                        frees[d].pop((info.get("all_courts") or {}).get(r, r), None)
            hours = club_hours(club, info)
            state = read_json(DATA / "state" / f"{key}.json", {"runs": [], "days": {}})
            courts = sorted(info["courts"].values())
            for d, free in frees.items():
                update_day(state["days"].setdefault(d.isoformat(), {}), courts,
                           blocks_for_day(hours, d, block), free, d, zone, now, ts)
            state["runs"] = [r for r in state["runs"] if r > ts - 40 * 86400] + [ts]
            state["courts"] = courts
            state["court_kinds"] = court_kinds(club, info)
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
