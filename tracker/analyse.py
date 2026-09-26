"""Finalise finished days into the block archive and build the dashboard and export data.

Block statuses in data/blocks/<club>/<YYYY-MM>.csv:
  free    - still available at the last check before it started (unsold)
  sold    - seen available, then taken before it started (a booking we watched happen)
  never   - never offered publicly while we watched (booked before it came into view,
            members' advance window, coaching, leagues, maintenance or other blocks)
  unknown - no check ran close enough before it started; excluded from all figures
Occupancy = (sold + never) / (free + sold + never).
Outputs: docs/data/summary.json (dashboard), docs/data/daily.csv, hourly.csv, courts.csv (export).
"""
from __future__ import annotations

import bisect
import csv
import statistics
from collections import defaultdict
from datetime import date, timedelta

from . import weather as wx
from .collect import club_hours
from .common import (DATA, DOCS_DATA, block_start, blocks_for_day, config_price_per_hour,
                     hours_by_weekday, hm_to_min, is_peak, load_config, now_utc, read_json,
                     tz, write_json)

FIELDS = ["date", "court", "time", "status", "rate", "lead_h"]
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------------------------------------------------------------- finalising days

def _append(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        with open(path, encoding="utf-8", newline="") as f:
            header = next(csv.reader(f), [])
        if header != FIELDS:  # older file without new columns: rewrite with the current header
            with open(path, encoding="utf-8", newline="") as f:
                old = list(csv.DictReader(f))
            with open(path, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
                w.writeheader()
                w.writerows(old)
    new = not path.exists()
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def finalise(cfg, club, zone, today):
    key = club["key"]
    path = DATA / "state" / f"{key}.json"
    state = read_json(path, None)
    if not state:
        return 0
    runs = sorted(state.get("runs", []))
    max_gap = int(cfg.get("max_gap_minutes", 180)) * 60
    courts = state.get("courts", [])
    info = read_json(DATA / "resolved.json", {}).get(key, {})
    done = 0
    for ds in sorted(list(state["days"].keys())):
        d = date.fromisoformat(ds)
        if d >= today:
            continue
        day = state["days"][ds]
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
                lead = round((t - e["s"]) / 3600, 1) if status == "sold" and e.get("s") else ""
                rows.append({"date": ds, "court": court, "time": hm, "status": status,
                             "rate": (e or {}).get("r", ""), "lead_h": lead})
        _append(DATA / "blocks" / key / f"{ds[:7]}.csv", rows)
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
        with open(f, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                if r["date"] >= since.isoformat():
                    rows.append(r)
    return rows


# ---------------------------------------------------------------- helpers

def pct(n, d):
    return round(n / d, 4) if d else None


def in_standard(cfg, d, hm, std):
    rng = std.get(d.weekday())
    return bool(rng) and rng[0] <= hm_to_min(hm) < rng[1]


class PriceBook:
    """Price for a block: listed price if seen, else median listed price for that weekday-type/hour."""

    def __init__(self, club, rows, state, block):
        self.club, self.block = club, block
        by, allr = defaultdict(list), []
        listed = [(r["date"], r["time"], r["rate"]) for r in rows if r["rate"] not in ("", None)]
        for ds, day in ((state or {}).get("days") or {}).items():
            for court in day.values():
                for hm, e in court.items():
                    if e.get("r") is not None:
                        listed.append((ds, hm, e["r"]))
        for ds, hm, rate in listed:
            d = date.fromisoformat(ds)
            by[("we" if d.weekday() >= 5 else "wd", hm[:2])].append(float(rate))
            allr.append(float(rate))
        self.med = {k: statistics.median(v) for k, v in by.items()}
        self.club_med = statistics.median(allr) if allr else None
        self.missing = False

    def rate(self, r, d):
        if r["rate"] not in ("", None):
            return float(r["rate"])
        k = ("we" if d.weekday() >= 5 else "wd", r["time"][:2])
        if k in self.med:
            return self.med[k]
        p = config_price_per_hour(self.club, d, r["time"])
        if p is not None:
            return p * self.block / 60
        if self.club_med is not None:
            return self.club_med
        self.missing = True
        return None


# ---------------------------------------------------------------- per-club aggregation

def aggregate(cfg, club, rows, state, kinds, today, block, std, days_open, wxd):
    real = float(club.get("realisation", 1.0))
    min_cov = float(cfg.get("min_day_coverage", 0.8))
    prices = PriceBook(club, rows, state, block)
    cover = defaultdict(lambda: [0, 0])
    for r in rows:
        cover[r["date"]][1] += 1
        cover[r["date"]][0] += r["status"] != "unknown"
    partial = {ds for ds, (k, n) in cover.items() if n and k / n < min_cov}

    # day -> scope -> counters ; hourly -> counters
    Z = lambda: defaultdict(float)  # noqa: E731
    daily = defaultdict(lambda: {"all": Z(), "std": Z()})
    hourly = defaultdict(Z)
    kind_tot = defaultdict(Z)        # (date, kind) -> counters (all hours)
    for r in rows:
        if r["status"] == "unknown" or r["date"] in partial:
            continue
        d = date.fromisoformat(r["date"])
        occ = r["status"] in ("sold", "never")
        peak = is_peak(cfg, d, r["time"])
        rt = prices.rate(r, d) if occ else None
        rev = rt * real if rt is not None else 0.0
        scopes = ["all"] + (["std"] if in_standard(cfg, d, r["time"], std) else [])
        for sc in scopes:
            a = daily[r["date"]][sc]
            a["total"] += 1
            a["peak_total" if peak else "off_total"] += 1
            if occ:
                a["occ"] += 1
                a["peak_occ" if peak else "off_occ"] += 1
                a[r["status"]] += 1
                a["rev"] += rev
                if peak:
                    a["peak_rev"] += rev
                else:
                    a["off_rev"] += rev
                if r.get("lead_h") not in ("", None):
                    a["lead_sum"] += float(r["lead_h"])
                    a["lead_n"] += 1
        h = hourly[(r["date"], int(r["time"][:2]))]
        h["total"] += 1
        h["occ"] += occ
        h["rev"] += rev
        h["std"] = 1 if in_standard(cfg, d, r["time"], std) else h.get("std", 0)
        h["peak"] = 1 if peak else h.get("peak", 0)
        k = kinds.get(r["court"], "unknown")
        kt = kind_tot[(r["date"], k)]
        kt["total"] += 1
        kt["occ"] += occ
    return daily, hourly, kind_tot, partial, prices.missing, cover


def window_metrics(daily, scope, start_iso, courts, block, days_open):
    t = defaultdict(float)
    n = 0
    for ds, v in daily.items():
        if ds >= start_iso:
            n += 1
            for k, x in v[scope].items():
                t[k] += x
    hrs = t["occ"] * block / 60
    peak_hrs, off_hrs = t["peak_occ"] * block / 60, t["off_occ"] * block / 60
    rev_ann = t["rev"] / n * days_open if n and t["rev"] else None
    return {
        "days": n,
        "occupancy": pct(t["occ"], t["total"]),
        "occupancy_peak": pct(t["peak_occ"], t["peak_total"]),
        "occupancy_offpeak": pct(t["off_occ"], t["off_total"]),
        "watched_bookings_share": pct(t["sold"], t["occ"]),
        "court_hours_sold_per_day": round(hrs / n, 1) if n else None,
        "bookable_court_hours_per_day": round(t["total"] * block / 60 / n, 1) if n else None,
        "revenue": round(t["rev"]) if t["rev"] else None,
        "revenue_annualised": round(rev_ann) if rev_ann else None,
        "revenue_per_court_annualised": round(rev_ann / courts) if rev_ann and courts else None,
        "avg_price_per_court_hour": round(t["rev"] / hrs, 2) if hrs and t["rev"] else None,
        "peak_price_per_court_hour": round(t["peak_rev"] / peak_hrs, 2) if peak_hrs and t["peak_rev"] else None,
        "offpeak_price_per_court_hour": round(t["off_rev"] / off_hrs, 2) if off_hrs and t["off_rev"] else None,
        "median_lead_h": round(t["lead_sum"] / t["lead_n"], 1) if t["lead_n"] else None,
        "_occ": t["occ"], "_total": t["total"], "_peak_occ": t["peak_occ"], "_peak_total": t["peak_total"],
        "_off_occ": t["off_occ"], "_off_total": t["off_total"],
    }


# ---------------------------------------------------------------- run

def run():
    cfg = load_config()
    zone = tz(cfg)
    block = int(cfg.get("block_minutes", 30))
    today = now_utc().astimezone(zone).date()
    days_open = int(cfg.get("days_open_per_year", 355))
    std = hours_by_weekday(cfg.get("standard_hours") or {"mon-fri": "07:00-22:00", "sat-sun": "08:00-22:00"})
    wxd = wx.update(cfg)
    status = read_json(DATA / "status.json", {})
    groups = cfg.get("groups") or {}
    out = {"generated": now_utc().isoformat(timespec="seconds"), "as_of": (today - timedelta(days=1)).isoformat(),
           "block_minutes": block, "days_open_per_year": days_open,
           "standard_hours_label": cfg.get("standard_hours_label", "07:00–22:00 weekdays, 08:00–22:00 weekends"),
           "peak_label": cfg.get("peak_label", "weekdays 17:00–22:00, weekends 08:00–20:00"),
           "groups": groups, "group_order": list(groups.keys()), "clubs": [], "market": {}, "by_kind": {}}
    daily_rows, hourly_rows, court_rows = [], [], []
    market_daily = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))  # group->scope->counters by window
    kind_all = defaultdict(lambda: defaultdict(float))

    for club in cfg["clubs"]:
        n = finalise(cfg, club, zone, today)
        if n:
            print(f"Finalised {n} day(s) for {club['name']}")
        state = read_json(DATA / "state" / f"{club['key']}.json", {})
        courts = len(state.get("courts", []))
        kinds = state.get("court_kinds") or {}
        rows = load_blocks(club["key"], today - timedelta(days=400))
        daily, hourly, kind_tot, partial, missing_price, cover = aggregate(
            cfg, club, rows, state, kinds, today, block, std, days_open, wxd)
        st = status.get(club["key"], {})
        grp = club.get("group", "local")
        entry = {"key": club["key"], "name": club["name"], "platform": club["platform"], "group": grp,
                 "courts": courts, "court_kinds": dict(sorted(
                     {k: list(kinds.values()).count(k) for k in set(kinds.values())}.items())),
                 "last_ok": st.get("last_ok"), "error": st.get("error"), "metrics": {}, "notes": []}
        for scope in ("all", "std"):
            entry["metrics"][scope] = {}
            for label, days in (("d7", 7), ("d28", 28), ("d90", 90)):
                m = window_metrics(daily, scope, (today - timedelta(days=days)).isoformat(), courts, block, days_open)
                mk = market_daily[(grp, label)][scope]
                for k in ("_occ", "_total", "_peak_occ", "_peak_total", "_off_occ", "_off_total"):
                    mk[k] += m.pop(k)
                entry["metrics"][scope][label] = m
        # daily series (last 120 days) with weather
        series = []
        for ds in sorted(daily)[-120:]:
            a, s_ = daily[ds]["all"], daily[ds]["std"]
            w = wxd.get(ds) or {}
            series.append({"date": ds, "occ": pct(a["occ"], a["total"]), "peak": pct(a["peak_occ"], a["peak_total"]),
                           "off": pct(a["off_occ"], a["off_total"]), "occ_std": pct(s_["occ"], s_["total"]),
                           "rev": round(a["rev"]) if a["rev"] else None, "rain_mm": w.get("rain_mm")})
        entry["daily"] = series
        # heatmap, last 28 days
        heat = defaultdict(lambda: [0, 0])
        for (ds, hh), v in hourly.items():
            if (today - date.fromisoformat(ds)).days <= 28:
                heat[(date.fromisoformat(ds).weekday(), hh)][0] += v["occ"]
                heat[(date.fromisoformat(ds).weekday(), hh)][1] += v["total"]
        hours = sorted({h for (_, h) in heat})
        entry["heatmap"] = {"hours": hours, "grid": [[pct(*heat[(dw, h)]) if (dw, h) in heat else None
                                                      for h in hours] for dw in range(7)]}
        for (ds, k), v in kind_tot.items():
            if (today - date.fromisoformat(ds)).days <= 28 and grp == "local":
                kind_all[k]["occ"] += v["occ"]
                kind_all[k]["total"] += v["total"]
        if partial:
            entry["notes"].append(f"{len(partial)} day(s) left out because checks covered less than "
                                  f"{int(float(cfg.get('min_day_coverage', 0.8)) * 100)}% of opening hours.")
        if missing_price:
            entry["notes"].append("Revenue incomplete: no prices seen yet for some hours.")
        out["clubs"].append(entry)

        # ---- export rows
        for ds in sorted(set(list(daily) + list(partial))):
            d = date.fromisoformat(ds)
            a, s_ = daily[ds]["all"], daily[ds]["std"]
            w = wxd.get(ds) or {}
            known, alln = cover.get(ds, (0, 0))
            daily_rows.append({
                "club": club["name"], "group": grp, "platform": club["platform"], "date": ds,
                "weekday": DAYS[d.weekday()], "courts": courts, "included": 0 if ds in partial else 1,
                "coverage": pct(known, alln),
                "bookable_court_hours": round(a["total"] * block / 60, 1),
                "occupied_court_hours": round(a["occ"] * block / 60, 1),
                "occupancy": pct(a["occ"], a["total"]), "occupancy_peak": pct(a["peak_occ"], a["peak_total"]),
                "occupancy_offpeak": pct(a["off_occ"], a["off_total"]),
                "occupancy_standard_hours": pct(s_["occ"], s_["total"]),
                "occupancy_standard_peak": pct(s_["peak_occ"], s_["peak_total"]),
                "occupancy_standard_offpeak": pct(s_["off_occ"], s_["off_total"]),
                "seen_booked_share": pct(a["sold"], a["occ"]),
                "avg_lead_hours": round(a["lead_sum"] / a["lead_n"], 1) if a["lead_n"] else None,
                "revenue_est_gbp": round(a["rev"], 2) if a["rev"] else 0,
                "rain_mm": w.get("rain_mm"), "rain_hours_7_22": w.get("rain_hours"), "max_temp_c": w.get("tmax_c")})
        for (ds, hh), v in sorted(hourly.items()):
            d = date.fromisoformat(ds)
            hourly_rows.append({"club": club["name"], "group": grp, "date": ds, "weekday": DAYS[d.weekday()],
                                "hour": f"{hh:02d}:00", "peak": int(v.get("peak", 0)),
                                "standard_hours": int(v.get("std", 0)),
                                "court_blocks": int(v["total"]), "occupied_blocks": int(v["occ"]),
                                "occupancy": pct(v["occ"], v["total"]), "revenue_est_gbp": round(v["rev"], 2),
                                "rain_mm": ((wxd.get(ds) or {}).get("hourly_mm") or {}).get(f"{hh:02d}")})
        for cname, k in sorted(kinds.items()):
            court_rows.append({"club": club["name"], "court": cname, "type": k})

    for (grp, label), scopes in market_daily.items():
        for scope, t in scopes.items():
            out["market"].setdefault(grp, {}).setdefault(scope, {})[label] = {
                "occupancy": pct(t["_occ"], t["_total"]), "occupancy_peak": pct(t["_peak_occ"], t["_peak_total"]),
                "occupancy_offpeak": pct(t["_off_occ"], t["_off_total"])}
    out["by_kind"] = {k: pct(v["occ"], v["total"]) for k, v in kind_all.items() if v["total"]}

    write_json(DOCS_DATA / "summary.json", out)
    _csv(DOCS_DATA / "daily.csv", daily_rows)
    _csv(DOCS_DATA / "hourly.csv", hourly_rows)
    _csv(DOCS_DATA / "courts.csv", court_rows)
    print("Wrote docs/data/summary.json, daily.csv, hourly.csv, courts.csv")


def _csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        if not rows:
            f.write("")
            return
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    run()
