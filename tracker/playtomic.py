"""Playtomic adapter.

Uses the endpoint the public playtomic.com website calls since Aug 2026:
  GET https://playtomic.com/api/clubs/availability?tenant_id=..&sport_id=PADEL&date=YYYY-MM-DD
It needs browser-like headers and the club page as Referer. Only FREE slots are
returned; anything within opening hours that is not offered counts as taken.
Court names are read from the club page when possible (best effort).
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from . import net
from .common import min_to_hm, parse_price

SITE = "https://playtomic.com"
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def club_url(club):
    return f"{SITE}/clubs/{club['slug']}"


def availability(club, tenant_id, d: date):
    return net.get(f"{SITE}/api/clubs/availability",
                   {"tenant_id": tenant_id, "sport_id": "PADEL", "date": d.isoformat()},
                   referer=club_url(club), accept="application/json, text/plain, */*", want="json")


def _page_info(club):
    """Best-effort: tenant id and {resource_id: name} from the club's public page."""
    try:
        html = net.get(club_url(club), accept="text/html,application/xhtml+xml")
    except net.FetchError as e:
        return None, {}, str(e)
    h = html.replace('\\"', '"')
    tids = Counter(re.findall(r'"tenant_id"\s*:\s*"(' + UUID + ')"', h))
    tenant = tids.most_common(1)[0][0] if tids else None
    names = {}
    name_pos = [(m.start(), m.group(1).strip()) for m in re.finditer(r'"name"\s*:\s*"([^"]{1,60})"', h)]
    for m in re.finditer(r'"resource_id"\s*:\s*"(' + UUID + ')"', h):
        rid = m.group(1)
        if rid in names:
            continue
        # prefer a name inside the same {...} object, else the nearest name within 400 characters
        o_start, o_end = h.rfind("{", 0, m.start()), h.find("}", m.end())
        inside = [n for pos, n in name_pos if o_start < pos < o_end and "{" not in h[o_start + 1:pos]]
        if inside:
            names[rid] = inside[0]
            continue
        near = sorted((abs(pos - m.start()), n) for pos, n in name_pos if abs(pos - m.start()) < 400)
        if near:
            names[rid] = near[0][1]
    return tenant, names, None


def resolve(club: dict, resolved: dict) -> dict:
    info = resolved.get(club["key"]) or {}
    if info.get("tenant_id") and info.get("refreshed", "") >= (date.today() - timedelta(days=7)).isoformat():
        return info
    tenant, names, page_err = _page_info(club)
    tenant = club.get("tenant_id") or tenant or info.get("tenant_id")
    if not tenant:
        raise RuntimeError(f"Could not find the Playtomic tenant id for {club['name']} "
                           f"({page_err or 'not on page'}). Add tenant_id to config.yaml.")
    info.update(tenant_id=tenant, refreshed=date.today().isoformat(),
                page_error=page_err, page_names=names or info.get("page_names", {}))
    info.setdefault("courts", {})
    for rid, nm in (info["page_names"] or {}).items():
        _add_court(club, info, rid, nm)
    resolved[club["key"]] = info
    return info


def _excluded(club, rid, name):
    ex = [e.lower() for e in club.get("exclude_courts") or []]
    return any(e in (name or "").lower() or e == rid.lower() for e in ex)


def _add_court(club, info, rid, name=None):
    name = name or (info.get("page_names") or {}).get(rid) or f"Court {rid[:4]}"
    taken = {n for r, n in info.get("all_courts", {}).items() if r != rid}
    if name in taken:                     # keep names unique
        name = f"{name} ({rid[:4]})"
    info.setdefault("all_courts", {})[rid] = name
    if _excluded(club, rid, name):
        info["courts"].pop(rid, None)
    else:
        info["courts"][rid] = name


def times_mode(club, resolved) -> str:
    if "times_are_local" in club:
        return "local" if club["times_are_local"] else "utc"
    return (resolved.get("_playtomic") or {}).get("times", "utc")


def detect_times(clubs, resolved, zone, hours_fn):
    """Work out whether the API returns UTC or local times, using a club with known opening hours.
    Only matters while UK clocks are on BST."""
    p = resolved.setdefault("_playtomic", {})
    if p.get("decided") and p["decided"] >= (date.today() - timedelta(days=14)).isoformat():
        return
    now = datetime.now(zone)
    if not now.utcoffset():
        return
    for club, info in clubs:
        hours = hours_fn(club, info)
        if not club.get("hours") or not hours:
            continue
        earliest = []
        for i in range(1, 8):
            d = now.date() + timedelta(days=i)
            if d.weekday() not in hours:
                continue
            raw = availability(club, info["tenant_id"], d)
            starts = [int(s["start_time"][:2]) * 60 + int(s["start_time"][3:5])
                      for r in raw or [] for s in r.get("slots") or [] if s.get("start_time")]
            if starts:
                earliest.append(min(starts) - hours[d.weekday()][0])
        if earliest:
            # if slots appear to start before opening when read as local time, they are UTC
            p["times"] = "utc" if min(earliest) < 0 else "local"
            p["decided"] = date.today().isoformat()
            p["evidence"] = f"{club['name']}: earliest slot vs opening (min) {sorted(earliest)[:3]}"
            return


def free_blocks(club, info, d: date, zone, block: int, mode: str = "utc"):
    """{court_name: {'HH:MM': rate_per_block or None}} of free blocks on local date d."""
    days = [d]  # clubs are closed around midnight, so one call covers the local day either way
    out: dict[str, dict[str, float | None]] = {}
    best_dur: dict[tuple, int] = {}
    for q in days:
        for res in availability(club, info["tenant_id"], q) or []:
            rid = res.get("resource_id")
            if not rid:
                continue
            if rid not in info.get("all_courts", {}):
                _add_court(club, info, rid)
            name = info["courts"].get(rid)
            if not name:
                continue
            for slot in res.get("slots") or []:
                st = slot.get("start_time") or ""
                dur = int(slot.get("duration") or 0)
                if not st or dur <= 0:
                    continue
                naive = datetime.fromisoformat(f"{res.get('start_date') or q.isoformat()}T{st[:8]}")
                local = naive.replace(tzinfo=zone) if mode == "local" else \
                    naive.replace(tzinfo=timezone.utc).astimezone(zone)
                if local.date() != d:
                    continue
                price = parse_price(slot.get("price"))
                rate = price / (dur / block) if price is not None else None
                m0 = local.hour * 60 + local.minute
                for m in range(m0, min(m0 + dur, 24 * 60), block):
                    hm, key = min_to_hm(m), (name, min_to_hm(m))
                    prev = best_dur.get(key)
                    if prev is None or abs(dur - 60) < abs(prev - 60):
                        best_dur[key] = dur
                        out.setdefault(name, {})[hm] = rate
    return out
