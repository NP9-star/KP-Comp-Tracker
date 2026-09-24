"""Playtomic adapter (public, unauthenticated endpoints used by the Playtomic web app).

Availability times from api.playtomic.io are returned in UTC and are converted to
club-local time here. Only *free* slots are returned by Playtomic; anything within
opening hours that is not offered is treated as taken.
"""
from __future__ import annotations

import time as _time
from datetime import date, datetime, timedelta, timezone

import requests

from .common import UA, hm_to_min, local_day_bounds_utc, min_to_hm, parse_price

API = "https://api.playtomic.io/v1"
_s = requests.Session()
_s.headers.update({"User-Agent": UA, "Accept": "application/json",
                   "X-Requested-With": "com.playtomic.web"})


def _get(path, params=None):
    r = _s.get(API + path, params=params, timeout=25)
    r.raise_for_status()
    _time.sleep(0.8)  # be gentle
    return r.json()


def search_tenants(lat, lon, radius_m=25000):
    return _get("/tenants", {"sport_id": "PADEL", "coordinate": f"{lat:.6f},{lon:.6f}",
                             "radius": int(radius_m), "size": 100})


def resolve(club: dict, resolved: dict) -> dict:
    """Return {'tenant_id', 'courts': {resource_id: name}, 'hours': {...} or None}."""
    info = resolved.get(club["key"]) or {}
    tenant_id = club.get("tenant_id") or info.get("tenant_id")
    if not tenant_id:
        s = club["search"]
        hits = search_tenants(s["lat"], s["lon"], s.get("radius_m", 25000))
        want = s["name"].lower()
        matches = [t for t in hits if want in (t.get("tenant_name") or "").lower()]
        if not matches:
            names = ", ".join(sorted(t.get("tenant_name", "?") for t in hits))
            raise RuntimeError(f"No Playtomic club matching '{s['name']}' near SL2. Found: {names}")
        tenant_id = matches[0]["tenant_id"]

    if info.get("tenant_id") == tenant_id and info.get("courts") and \
            info.get("refreshed", "") >= (date.today() - timedelta(days=7)).isoformat():
        return info

    tenant = _get(f"/tenants/{tenant_id}")
    resources = tenant.get("resources") or []
    if not resources:
        try:
            resources = _get(f"/tenants/{tenant_id}/resources")
        except requests.HTTPError:
            resources = []
    excl = [e.lower() for e in club.get("exclude_courts") or []]
    courts = {}
    for r in resources:
        sport = (r.get("sport_id") or "PADEL").upper()
        name = (r.get("name") or r.get("resource_id")).strip()
        if sport != "PADEL" or any(e in name.lower() for e in excl):
            continue
        if r.get("is_active") is False:
            continue
        courts[r["resource_id"]] = name

    info = {
        "tenant_id": tenant_id,
        "tenant_name": tenant.get("tenant_name"),
        "courts": courts,
        "hours": _hours_from_tenant(tenant),
        "refreshed": date.today().isoformat(),
    }
    resolved[club["key"]] = info
    return info


_DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]


def _hours_from_tenant(tenant: dict):
    """Best-effort read of opening hours; shape varies, so be defensive."""
    oh = tenant.get("opening_hours")
    if not isinstance(oh, dict):
        return None
    out = {}
    for i, day in enumerate(_DAYS):
        v = oh.get(day) or oh.get(day.lower())
        if not isinstance(v, dict):
            continue
        o, c = v.get("opening_time"), v.get("closing_time")
        if o and c:
            out[["mon", "tue", "wed", "thu", "fri", "sat", "sun"][i]] = f"{o[:5]}-{c[:5]}"
    return out or None


def free_blocks(club, info, d: date, zone, block: int) -> dict[str, dict[str, float | None]]:
    """{court_name: {'HH:MM': rate_per_block or None}} of free 30-min blocks on local date d."""
    start_utc, end_utc = local_day_bounds_utc(d, zone)
    data = _get("/availability", {
        "sport_id": "PADEL",
        "tenant_id": info["tenant_id"],
        "start_min": start_utc.strftime("%Y-%m-%dT%H:%M:%S"),
        "start_max": end_utc.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    out: dict[str, dict[str, float | None]] = {}
    best_dur: dict[tuple, int] = {}
    for res in data or []:
        name = info["courts"].get(res.get("resource_id"))
        if not name:
            continue
        for slot in res.get("slots") or []:
            st = slot.get("start_time") or ""
            dur = int(slot.get("duration") or 0)
            if not st or dur <= 0:
                continue
            start = datetime.fromisoformat(f"{res['start_date']}T{st[:8]}")
            if club.get("times_are_local"):
                local = start.replace(tzinfo=zone)
            else:
                local = start.replace(tzinfo=timezone.utc).astimezone(zone)
            if local.date() != d:
                continue
            price = parse_price(slot.get("price"))
            rate = price / (dur / block) if price is not None else None
            m0 = local.hour * 60 + local.minute
            for m in range(m0, m0 + dur, block):
                if m >= 24 * 60:
                    break
                hm = min_to_hm(m)
                key = (name, hm)
                # prefer the rate implied by a 60-minute booking where available
                prev = best_dur.get(key)
                if prev is None or abs(dur - 60) < abs(prev - 60):
                    best_dur[key] = dur
                    out.setdefault(name, {})[hm] = rate
                else:
                    out.setdefault(name, {}).setdefault(hm, rate)
    return out
