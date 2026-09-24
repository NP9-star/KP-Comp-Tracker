"""MATCHi adapter.

MATCHi has no public API; this reads the same schedule fragment the booking page
loads (/book/schedule). Free cells carry class "slot free" and a title such as
"Available<br>Court 1<br> 10:00 - 11:00". Prices are not on the schedule, so
revenue comes from the price table in config.yaml.
"""
from __future__ import annotations

import re
import time as _time
from datetime import date
from html import unescape

import requests

from .common import UA, hm_to_min, min_to_hm

BASE = "https://www.matchi.se"
_s = requests.Session()
_s.headers.update({"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})

TD_RE = re.compile(r"<td\b([^>]*)>", re.I)
ATTR_RE = re.compile(r'(\w[\w-]*)\s*=\s*"([^"]*)"')
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})")
PADEL_SPORT_GUESSES = [5, 6, 7, 8, 9, 10, 4, 3, 2, 1]


def find_facilities():
    r = _s.post(f"{BASE}/facilities/findFacilities",
                data={"asJson": "true", "q": "", "lat": "51.54", "lng": "-0.65"}, timeout=30)
    r.raise_for_status()
    data = r.json()
    return (data.get("facilities") or []) + (data.get("restOfFacilities") or [])


def schedule_html(facility_id: int, d: date, sport: int, indoor: bool | None) -> str:
    params = {"wl": "", "facilityId": facility_id, "date": d.isoformat(), "sport": sport,
              "week": "", "year": ""}
    if indoor is not None:
        params["indoor"] = "true" if indoor else "false"
    r = _s.get(f"{BASE}/book/schedule", params=params, timeout=30)
    r.raise_for_status()
    _time.sleep(0.8)
    return r.text


def parse_cells(html: str):
    """Yield (court, start_min, end_min, is_free, classes) for every schedule cell with a time."""
    for m in TD_RE.finditer(html):
        attrs = dict((k.lower(), unescape(v)) for k, v in ATTR_RE.findall(m.group(1)))
        cls = attrs.get("class", "")
        if "slot" not in cls.split():
            continue
        title = attrs.get("title") or attrs.get("data-original-title") or ""
        parts = [p.strip() for p in re.split(r"<br\s*/?>|\n", title) if p.strip()]
        tm = TIME_RE.search(title)
        if not tm:
            continue
        start = int(tm.group(1)) * 60 + int(tm.group(2))
        end = int(tm.group(3)) * 60 + int(tm.group(4))
        if end <= start:
            end += 24 * 60
        court = None
        for p in parts:
            if not TIME_RE.search(p) and p.lower() not in ("available", "ledig", "booked", "bokad", "not available"):
                court = p
        if not court and len(parts) >= 2:
            court = parts[1]
        yield court or "Court", start, end, ("free" in cls.split()), cls


def resolve(club: dict, resolved: dict) -> dict:
    info = resolved.get(club["key"]) or {}
    if info.get("facility_id") and info.get("sport"):
        return info
    facilities = find_facilities()
    short = club.get("shortname", "").lower()
    match = [f for f in facilities if (f.get("shortname") or "").lower() == short]
    if not match:
        raise RuntimeError(f"MATCHi facility '{short}' not found")
    fid = int(match[0]["id"])

    # Detect the padel sport id by finding which one returns schedule cells
    probe = date.today()
    sport, courts = None, set()
    for sp in PADEL_SPORT_GUESSES:
        cells = []
        for indoor in (None, False, True):
            cells = list(parse_cells(schedule_html(fid, probe, sp, indoor)))
            if cells:
                break
        if cells:
            sport = sp
            courts = {c[0] for c in cells}
            break
    if sport is None:
        raise RuntimeError(f"No schedule cells found for MATCHi facility {fid}; run discover and send the output")
    info = {"facility_id": fid, "sport": sport, "courts": {c: c for c in sorted(courts)},
            "name": match[0].get("name")}
    resolved[club["key"]] = info
    return info


def free_blocks(club, info, d: date, zone, block: int) -> dict[str, dict[str, float | None]]:
    out: dict[str, dict[str, float | None]] = {}
    html = schedule_html(info["facility_id"], d, info["sport"], None)
    cells = list(parse_cells(html))
    if not cells:
        for indoor in (False, True):
            cells += list(parse_cells(schedule_html(info["facility_id"], d, info["sport"], indoor)))
    for court, start, end, is_free, _ in cells:
        info["courts"].setdefault(court, court)
        if not is_free:
            continue
        for m in range(start, end, block):
            if m < 24 * 60:
                out.setdefault(court, {})[min_to_hm(m)] = None
    return out
