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


DAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
KIND_RE = re.compile(r"\b(indoor|outdoor|roofed|covered)\s*,\s*(single|double)\b", re.I)


def _page_text(html):
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", "\n", t)
    t = re.sub(r"&amp;", "&", t)
    return re.sub(r"\n\s*\n+", "\n", t)


def _hours_from_text(text):
    """{'mon': '06:00-23:00', ...} from the 'Opening hours' list, if all seven days are there."""
    out = {}
    for i, day in enumerate(DAYS_EN):
        m = re.search(day + r"\s*\n?\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", text)
        if m:
            end = "24:00" if m.group(2) in ("00:00", "0:00") else m.group(2)
            out[["mon", "tue", "wed", "thu", "fri", "sat", "sun"][i]] = f"{m.group(1)}-{end}"
    return out if len(out) == 7 else None


def _courts_from_text(text):
    """[(name, kind, size)] from the booking grid, e.g. 'Court 1 (Singles Court)' / 'indoor, single, panoramic'."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    out, seen = [], set()
    for i, l in enumerate(lines):
        m = KIND_RE.match(l)
        if m and i > 0:
            name = lines[i - 1]
            if name not in seen and len(name) < 60:
                seen.add(name)
                out.append((name, m.group(1).lower(), m.group(2).lower()))
    return out


def _page_info(club):
    """Best-effort read of the public club page: tenant id, {resource_id: name}, courts, hours."""
    try:
        html = net.get(club_url(club), accept="text/html,application/xhtml+xml")
    except net.FetchError as e:
        return None, {}, [], None, str(e), ""
    h = html.replace('\\"', '"')
    tids = Counter(re.findall(r'"tenant_?[iI]d"\s*:\s*"(' + UUID + ')"', h))
    tenant = tids.most_common(1)[0][0] if tids else None
    names = {}
    name_pos = [(m.start(), m.group(1).strip()) for m in re.finditer(r'"name"\s*:\s*"([^"]{1,60})"', h)]
    for m in re.finditer(r'"(?:resource_?[iI]d|resourceId)"\s*:\s*"(' + UUID + ')"', h):
        rid = m.group(1)
        if rid in names:
            continue
        o_start, o_end = h.rfind("{", 0, m.start()), h.find("}", m.end())
        inside = [n for pos, n in name_pos if o_start < pos < o_end and "{" not in h[o_start + 1:pos]]
        if inside:
            names[rid] = inside[0]
            continue
        near = sorted((abs(pos - m.start()), n) for pos, n in name_pos if abs(pos - m.start()) < 400)
        if near:
            names[rid] = near[0][1]
    text = _page_text(html)
    return tenant, names, _courts_from_text(text), _hours_from_text(text), None, html


def resolve(club: dict, resolved: dict) -> dict:
    info = resolved.get(club["key"]) or {}
    if info.get("tenant_id") and "page_courts" in info and \
            info.get("refreshed", "") >= (date.today() - timedelta(days=7)).isoformat():
        for rid in list(info.get("courts", {})):          # apply any new exclusions
            if _excluded(club, rid, info["courts"][rid]) or rid in (info.get("auto_excluded") or []):
                info["courts"].pop(rid)
        return info
    tenant, names, page_courts, page_hours, page_err, _ = _page_info(club)
    tenant = club.get("tenant_id") or tenant or info.get("tenant_id")
    if not tenant:
        raise RuntimeError(f"Could not find the Playtomic tenant id for {club['name']} "
                           f"({page_err or 'not on page'}). Add tenant_id to config.yaml.")
    info.update(tenant_id=tenant, refreshed=date.today().isoformat(), page_error=page_err,
                page_names=names or info.get("page_names", {}),
                page_courts=page_courts or info.get("page_courts", []),
                hours=page_hours or info.get("hours"))
    kinds = {k for _, k, _ in info["page_courts"]}
    info["singles_on_page"] = sum(1 for _, _, sz in info["page_courts"] if sz == "single")
    if len(kinds) == 1:
        info["club_kind"] = next(iter(kinds))
    info.setdefault("courts", {})
    for rid, nm in (info["page_names"] or {}).items():
        _add_court(club, info, rid, nm)
    resolved[club["key"]] = info
    return info


def court_kind(club, info, rid_or_name):
    """indoor / covered / outdoor for a court, from the page, else the club default in config."""
    name = (info.get("all_courts") or {}).get(rid_or_name, rid_or_name)
    for n, k, _ in info.get("page_courts") or []:
        if n == name:
            return normalise_kind(k)
    return normalise_kind(info.get("club_kind") or club.get("court_type") or "")


def normalise_kind(k):
    k = (k or "").lower()
    if "roof" in k or "cover" in k or "canopy" in k:
        return "covered"
    if "indoor" in k:
        return "indoor"
    if "outdoor" in k:
        return "outdoor"
    return "unknown"


def auto_exclude_singles(club, info):
    """If the club page lists N singles courts but names can't be matched to court ids, leave out
    the N courts that are consistently priced below the others at the same start time
    (singles courts are cheaper). Decided once, when every court has enough comparisons."""
    n = int(info.get("singles_on_page") or 0)
    if not n or club.get("include_singles") or info.get("auto_decided"):
        return
    allc = info.get("all_courts") or {}
    if any("single" in nm.lower() for nm in allc.values()):
        info["auto_decided"] = True       # names matched; exclude_courts handles it
        return
    if sum(1 for r, nm in allc.items() if _excluded(club, r, nm)) >= n:
        info["auto_decided"] = True       # already handled in config
        return
    ratios = {r: sorted(v)[len(v) // 2] for r, v in (info.get("rid_ratio") or {}).items() if len(v) >= 20}
    if len(ratios) < len(allc) or len(ratios) <= n:
        return                            # keep collecting evidence
    cheapest = sorted(ratios, key=ratios.get)[:n]
    picked = [r for r in cheapest if ratios[r] < 0.92]
    info["auto_excluded"] = picked
    info["auto_decided"] = True
    for r in picked:
        info["courts"].pop(r, None)


def _excluded(club, rid, name):
    ex = [e.lower() for e in club.get("exclude_courts") or []]
    return any(e in (name or "").lower() or e == rid.lower() for e in ex)


def _add_court(club, info, rid, name=None):
    name = name or (info.get("page_names") or {}).get(rid) or f"Court {rid[:4]}"
    taken = {n for r, n in info.get("all_courts", {}).items() if r != rid}
    if name in taken:                     # keep names unique
        name = f"Court {rid[:8]}" if name.startswith("Court ") and len(name) <= 10 else f"{name} ({rid[:8]})"
    if name in taken:
        name = f"Court {rid}"
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
    by_start: dict[tuple, dict] = {}
    for q in days:
        for res in availability(club, info["tenant_id"], q) or []:
            rid = res.get("resource_id")
            if not rid:
                continue
            if rid not in info.get("all_courts", {}):
                _add_court(club, info, rid)
            name = None if rid in (info.get("auto_excluded") or []) else info["courts"].get(rid)
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
                if not name:
                    rate0 = price / (dur / block) if price is not None else None
                    if rate0 is not None and dur == 60:
                        by_start.setdefault((local.hour, local.minute), {})[rid] = rate0
                    continue
                rate = price / (dur / block) if price is not None else None
                if rate is not None and dur == 60:
                    by_start.setdefault((local.hour, local.minute), {})[rid] = rate
                m0 = local.hour * 60 + local.minute
                for m in range(m0, min(m0 + dur, 24 * 60), block):
                    hm, key = min_to_hm(m), (name, min_to_hm(m))
                    prev = best_dur.get(key)
                    if prev is None or abs(dur - 60) < abs(prev - 60):
                        best_dur[key] = dur
                        out.setdefault(name, {})[hm] = rate
    for rates in by_start.values():
        if len(rates) >= 2:
            med = sorted(rates.values())[len(rates) // 2]
            for rid, r in rates.items():
                rr = info.setdefault("rid_ratio", {}).setdefault(rid, [])
                rr.append(round(r / med, 3) if med else 1.0)
                del rr[:-200]
    return out
