"""Nettla adapter (the booking widget some clubs embed on their own website, e.g. PadelStars).

The widget calls a public endpoint:
  POST https://europe-west1-nettle-prod-2024.cloudfunctions.net/rpc/availability/search
  {"json": {"clubId": "...", "startDateTime": "<UTC ISO>", "endDateTime": "<UTC ISO>"}}
It returns {"json": {"availableSlots": {"<UTC start>": [{courtId, name, type, size, sport,
durations: [{minutes, prices: [{price (pence), splits}]}]}]}}} - only times a court can be
started. Anything within opening hours that is not offered counts as taken.

Used alongside another platform: a club's `nettla` setting adds the courts it lists
(e.g. PadelStars Egham books courts 1-2 here and court 3 on Playtomic).
"""
from __future__ import annotations

import time as _time
from datetime import date, datetime, time, timedelta, timezone

import requests

from .common import min_to_hm
from .net import BROWSER_UA, PAUSE

API = "https://europe-west1-nettle-prod-2024.cloudfunctions.net/rpc/availability/search"


def search(cfg_n: dict, d: date, zone):
    start = datetime.combine(d, time(0, 0), tzinfo=zone).astimezone(timezone.utc)
    end = start + timedelta(days=1) - timedelta(milliseconds=1)
    body = {"json": {"clubId": cfg_n["club_id"],
                     "startDateTime": start.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                     "endDateTime": end.isoformat(timespec="milliseconds").replace("+00:00", "Z")}}
    site = cfg_n.get("site", "https://padelstars.co.uk/")
    err = None
    for attempt in range(2):
        try:
            r = requests.post(API, json=body, timeout=30,
                              headers={"User-Agent": BROWSER_UA, "Referer": site, "Origin": site.rstrip("/"),
                                       "Accept": "application/json"})
            _time.sleep(PAUSE)
            if r.status_code == 200:
                return (r.json().get("json") or {}).get("availableSlots") or {}
            err = f"HTTP {r.status_code} {r.text[:160]}"
        except requests.RequestException as e:
            err = f"{type(e).__name__}: {e}"
        _time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Nettla availability failed: {err}")


def _kind(t):
    t = (t or "").lower()
    return "covered" if ("cover" in t or "roof" in t) else "indoor" if "indoor" in t else "outdoor" if "outdoor" in t else "unknown"


def free_blocks(club, info, d: date, zone, block: int):
    """{court name: {'HH:MM': rate per block}} for local date d; records court names and types in info."""
    cfg_n = club["nettla"]
    courts = info.setdefault("nettla_courts", {})
    kinds = info.setdefault("nettla_kinds", {})
    out: dict[str, dict[str, float | None]] = {}
    for t, offers in search(cfg_n, d, zone).items():
        local = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(zone)
        if local.date() != d:
            continue
        for o in offers or []:
            if (o.get("sport") or "Padel").lower() != "padel" or "single" in (o.get("size") or "").lower():
                continue
            name = o.get("name") or f"Court {o.get('courtId', '')[:6]}"
            courts[o.get("courtId") or name] = name
            kinds[name] = _kind(o.get("type"))
            durs = sorted((x for x in o.get("durations") or [] if x.get("minutes")),
                          key=lambda x: abs(int(x["minutes"]) - 60))
            if not durs:
                continue
            dur = int(durs[0]["minutes"])
            full = [p for p in durs[0].get("prices") or [] if not p.get("splits")]
            price = full[0]["price"] / 100 if full and full[0].get("price") is not None else None
            rate = price / (dur / block) if price is not None else None
            m0 = local.hour * 60 + local.minute
            for m in range(m0, min(m0 + dur, 24 * 60), block):
                cur = out.setdefault(name, {})
                if cur.get(min_to_hm(m)) is None:
                    cur[min_to_hm(m)] = rate
    return out
