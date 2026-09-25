"""MATCHi adapter.

Reads the schedule fragment the booking page loads (/book/schedule). Free cells carry
class "slot free" and a title like "Available<br>Court 1<br> 10:00 - 11:00".
The facility id is read from the club's public page (or set facility_id in config).
Prices are not on the schedule, so revenue uses the price table in config.yaml.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, timedelta
from html import unescape

from . import net
from .common import DATA, min_to_hm

DEBUG = {"on": False, "n": 0}   # discover switches this on to save raw responses


def _save(name, text):
    if DEBUG["on"]:
        d = DATA / "debug"
        d.mkdir(parents=True, exist_ok=True)
        DEBUG["n"] += 1
        (d / f"{DEBUG['n']:02d}_{name}").write_text(text or "", encoding="utf-8")

BASE = "https://www.matchi.se"
TD_RE = re.compile(r"<td\b([^>]*)>", re.I)
ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})")
SPORT_GUESSES = [5, 6, 7, 8, 9, 10, 11, 12, 4, 3, 2, 1]
FACILITY_PATTERNS = [
    r'facilityId["\']?\s*[:=]\s*["\']?(\d{2,7})',
    r'facilityId=(\d{2,7})',
    r'data-facility-?id=["\'](\d{2,7})',
    r'"facility_?[iI]d"\s*:\s*"?(\d{2,7})',
    r'/facilities/(\d{2,7})[/"\'?]',
]


def facility_url(club):
    return f"{BASE}/facilities/{club['shortname']}"


def schedule_html(club, facility_id: int, d: date, sport: int, indoor):
    params = {"wl": "", "facilityId": facility_id, "date": d.isoformat(), "sport": sport,
              "week": "", "year": ""}
    if indoor is not None:
        params["indoor"] = "true" if indoor else "false"
    html = net.get(f"{BASE}/book/schedule", params, referer=facility_url(club),
                   accept="text/html, */*; q=0.01")
    _save(f"{club['key']}_schedule_sport{sport}_indoor{indoor}.html", html)
    return html


def parse_cells(html: str):
    """Yield (court, start_min, end_min, is_free, classes) for each timed schedule cell."""
    for m in TD_RE.finditer(html or ""):
        attrs = {k.lower(): unescape(v) for k, v in ATTR_RE.findall(m.group(1))}
        cls = attrs.get("class", "")
        if "slot" not in cls.split():
            continue
        title = attrs.get("title") or attrs.get("data-original-title") or attrs.get("data-title") or ""
        tm = TIME_RE.search(title)
        if not tm:
            continue
        start = int(tm.group(1)) * 60 + int(tm.group(2))
        end = int(tm.group(3)) * 60 + int(tm.group(4))
        if end <= start:
            end += 24 * 60
        parts = [p.strip() for p in re.split(r"<br\s*/?>|\n", title) if p.strip()]
        court = next((p for p in parts[1:] if not TIME_RE.search(p)), None) or \
            next((p for p in parts if not TIME_RE.search(p)), "Court")
        yield court, start, end, ("free" in cls.split()), cls


def facility_page(club):
    html = net.get(facility_url(club), {"lang": "en"}, accept="text/html,application/xhtml+xml")
    _save(f"{club['key']}_facility_page.html", html)
    return html


def sports_on_page(html):
    """Sport ids the page mentions, e.g. sport=5, data-sport="5", sportId: 5."""
    found = re.findall(r'sport(?:Id|_id)?["\']?\s*[:=]\s*["\']?(\d{1,3})\b', html or "", re.I)
    found += re.findall(r'data-sport(?:-id)?=["\'](\d{1,3})', html or "", re.I)
    out = []
    for x in found:
        if int(x) not in out:
            out.append(int(x))
    return out


def find_facility_id(club, html=None):
    if club.get("facility_id"):
        return int(club["facility_id"])
    html = html or facility_page(club)
    votes = Counter()
    for pat in FACILITY_PATTERNS:
        votes.update(re.findall(pat, html))
    if not votes:
        raise RuntimeError(f"Facility id not found on {facility_url(club)}; add facility_id to config.yaml")
    return int(votes.most_common(1)[0][0])


def resolve(club: dict, resolved: dict) -> dict:
    info = resolved.get(club["key"]) or {}
    if info.get("facility_id") and info.get("sport"):
        return info
    page = None if club.get("facility_id") else facility_page(club)
    fid = find_facility_id(club, page)
    probe = date.today() + timedelta(days=1)
    guesses = [club["sport_id"]] if club.get("sport_id") else \
        sports_on_page(page) + [s for s in SPORT_GUESSES if s not in sports_on_page(page)]
    sport, courts, first = None, set(), None
    for sp in guesses:
        for indoor in (None, False, True):
            html = schedule_html(club, fid, probe, sp, indoor)
            first = first or html
            cells = list(parse_cells(html))
            if cells:
                sport, courts = sp, {c[0] for c in cells}
                break
        if sport is not None:
            break
    if sport is None:
        tds = Counter(re.findall(r'<td[^>]*class="([^"]*)"', first or ""))
        raise RuntimeError(
            f"No schedule cells found for MATCHi facility {fid} (sports tried {guesses[:6]}...). "
            f"Response length {len(first or '')}; cell classes {dict(tds.most_common(5))}; "
            f"starts: {' '.join((first or '')[:300].split())}")
    info = {"facility_id": fid, "sport": sport, "courts": {c: c for c in sorted(courts)}}
    resolved[club["key"]] = info
    return info


def free_blocks(club, info, d: date, zone, block: int, mode=None):
    out: dict[str, dict[str, float | None]] = {}
    cells = list(parse_cells(schedule_html(club, info["facility_id"], d, info["sport"], None)))
    for court, start, end, is_free, _ in cells:
        info["courts"].setdefault(court, court)
        if is_free:
            for m in range(start, min(end, 24 * 60), block):
                out.setdefault(court, {})[min_to_hm(m)] = None
    return out
