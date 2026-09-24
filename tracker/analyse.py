"""Finalise finished days into the block archive and build docs/data/summary.json.

Block statuses in data/blocks/<club>/<YYYY-MM>.csv:
  free    - still available at the last check before it started (unsold)
  sold    - seen available, then taken before it started (a booking we watched happen)
  never   - never offered publicly while we watched (booked earlier, members-only window,
            coaching, leagues, maintenance or other blocks)
  unknown - no check ran close enough before it started; excluded from all figures
Occupancy = (sold + never) / (free + sold + never).
"""
from __future__ import annotations

import bisect
import csv
import statistics
from collections import defaultdict
from datetime import date, timedelta

from .collect import club_hours
from .common import (DATA, DOCS_DATA, block_start, blocks_for_day, config_price_per_hour,
                     is_peak, load_config, now_utc, read_json, tz, write_json)

FIELDS = ["date", "court", "time", "status", "rate"]


def finalise(cfg, club, zone, today):
    key = club["key"]
    path = DATA / "state" / f"{key}.json"
    state = read_json(path, None)
    if not state:
        return 0
    runs = sorted(state.get("runs", []))
    max_gap = int(cfg.get("max_gap_minutes", 180)) * 60
    courts = state.get("courts", [])
    done = 0
    for ds in sorted(list(state["days"].keys())):
        d = date.fromisoformat(ds)
        if d >= today:
            continue
        day = state["days"][ds]
        info = read_json(DATA / "resolved.json", {}).get(key, {})
        blocks = blocks_for_day(club_hours(club, info), d, int(cfg.get("block_minutes", 30)))
        rows = []
        for court in courts:
            cs = day.get(court, {})
            for hm in blocks:
                t = block_start(d, hm, zone).timestamp()
                i = bisect.bisect_left(runs, t) - 1
                e = cs.get(hm)
                if i < 0 or t - runs[i] > max_gap:
                    status = "unknown"
                elif e and e.get("l") == 1:
                    status = "free"
                elif e:
                    status = "sold"
                else:
                    status = "never"
                rows.append({"date": ds, "court": court, "time": hm, "status": status,
                             "rate": (e or {}).get("r", "")})
        out = DATA / "blocks" / key / f"{ds[:7]}.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        new = not out.exists()
        with open(out, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerows(rows)
        del state["days"][ds]
        done += 1
    if done:
        write_json(path, state)
    return done


def load_blocks(key, since: date):
    rows = []
    folder = DATA / "blocks" / key
    if not folder.exists():
        return rows
    for f in sorted(folder.glob("*.csv")):
        if f.stem < since.isoformat()[:7]:
            continue
        with open(f) as fh:
            for r in csv.DictReader(fh):
                if r["date"] >= since.isoformat():
                    rows.append(r)
    return rows


def pct(n, d):
    return round(n / d, 4) if d else None


def summarise(cfg, club, rows, courts_now, today, block):
    real = float(club.get("realisation", 1.0))
    # --- price fallbacks from observed listed prices
    by_slot = defaultdict(list)
    all_rates = []
    for r in rows:
        if r["rate"] not in ("", None):
            d = date.fromisoformat(r["date"])
            k = ("we" if d.weekday() >= 5 else "wd", r["time"][:2])
            by_slot[k].append(float(r["rate"]))
            all_rates.append(float(r["rate"]))
    med = {k: statistics.median(v) for k, v in by_slot.items()}
    club_med = statistics.median(all_rates) if all_rates else None
    missing_price = False

    def rate_for(r, d):
        nonlocal missing_price
        if r["rate"] not in ("", None):
            return float(r["rate"])
        k = ("we" if d.weekday() >= 5 else "wd", r["time"][:2])
        if k in med:
            return med[k]
        p = config_price_per_hour(club, d, r["time"])
        if p is not None:
            return p * block / 60
        if club_med is not None:
            return club_med
        missing_price = True
        return None

    daily = defaultdict(lambda: defaultdict(float))
    heat = defaultdict(lambda: [0, 0])  # (dow, hour) -> [occupied, total]
    for r in rows:
        if r["status"] == "unknown":
            continue
        d = date.fromisoformat(r["date"])
        occ = r["status"] in ("sold", "never")
        peak = is_peak(cfg, d, r["time"])
        agg = daily[r["date"]]
        agg["total"] += 1
        agg["peak_total" if peak else "off_total"] += 1
        if occ:
            agg["occ"] += 1
            agg["peak_occ" if peak else "off_occ"] += 1
            agg[r["status"]] += 1
            rt = rate_for(r, d)
            if rt is not None:
                agg["rev"] += rt * real
        if (today - d).days <= 28:
            h = heat[(d.weekday(), int(r["time"][:2]))]
            h[0] += occ
            h[1] += 1

    def window(days):
        start = (today - timedelta(days=days)).isoformat()
        sel = {k: v for k, v in daily.items() if k >= start}
        t = defaultdict(float)
        for v in sel.values():
            for k2, x in v.items():
                t[k2] += x
        n = len(sel)
        hours_sold = t["occ"] * block / 60
        rev_ann = t["rev"] / n * 365 if n and t["rev"] else None
        return {
            "days": n,
            "occupancy": pct(t["occ"], t["total"]),
            "occupancy_peak": pct(t["peak_occ"], t["peak_total"]),
            "occupancy_offpeak": pct(t["off_occ"], t["off_total"]),
            "watched_bookings_share": pct(t["sold"], t["occ"]),
            "court_hours_sold_per_day": round(hours_sold / n, 1) if n else None,
            "revenue": round(t["rev"]) if t["rev"] else None,
            "revenue_annualised": round(rev_ann) if rev_ann else None,
            "revenue_per_court_annualised": round(rev_ann / courts_now) if rev_ann and courts_now else None,
            "avg_price_per_court_hour": round(t["rev"] / hours_sold, 2) if hours_sold and t["rev"] else None,
        }

    series = []
    for ds in sorted(daily)[-120:]:
        v = daily[ds]
        series.append({"date": ds, "occ": pct(v["occ"], v["total"]),
                       "peak": pct(v["peak_occ"], v["peak_total"]),
                       "off": pct(v["off_occ"], v["off_total"]),
                       "rev": round(v["rev"]) if v["rev"] else None})
    hours = sorted({h for (_, h) in heat})
    grid = [[pct(*heat[(dw, h)]) if (dw, h) in heat else None for h in hours] for dw in range(7)]
    notes = []
    if missing_price or (club["platform"] == "matchi" and not any(
            (p.get("per_hour") not in (None, "")) for p in club.get("prices") or [])):
        notes.append("Revenue not estimated: add court prices for this club in config.yaml.")
    return {"d7": window(7), "d28": window(28), "d90": window(90),
            "daily": series, "heatmap": {"hours": hours, "grid": grid}, "notes": notes}


def run():
    cfg = load_config()
    zone = tz(cfg)
    block = int(cfg.get("block_minutes", 30))
    today = now_utc().astimezone(zone).date()
    status = read_json(DATA / "status.json", {})
    out = {"generated": now_utc().isoformat(timespec="seconds"), "as_of": (today - timedelta(days=1)).isoformat(),
           "block_minutes": block, "clubs": []}
    for club in cfg["clubs"]:
        n = finalise(cfg, club, zone, today)
        if n:
            print(f"Finalised {n} day(s) for {club['name']}")
        state = read_json(DATA / "state" / f"{club['key']}.json", {})
        courts = len(state.get("courts", []))
        rows = load_blocks(club["key"], today - timedelta(days=120))
        s = summarise(cfg, club, rows, courts, today, block)
        st = status.get(club["key"], {})
        out["clubs"].append({"key": club["key"], "name": club["name"], "platform": club["platform"],
                             "courts": courts, "last_ok": st.get("last_ok"), "error": st.get("error"), **s})
    write_json(DOCS_DATA / "summary.json", out)
    print("Wrote docs/data/summary.json")


if __name__ == "__main__":
    run()
