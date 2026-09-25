"""PadelOS adapter (UK Padel's booking system, player.padelos.co).

The public bookings page calls:
  POST https://api.padelos.co/customers/searchByDate   {"date": "YYYY-MM-DD", "sport": "padel", ...}
with x-clubos-* headers and no login. It returns every club in the company, each with an
"availability" list of FREE slots. This module reads that list; anything within opening
hours that is not offered counts as taken.
"""
from __future__ import annotations

import json
import re
import time as _time
from datetime import date, datetime, timedelta, timezone

import requests

from .common import DATA, hm_to_min, min_to_hm, parse_price
from .net import BROWSER_UA, FetchError

API = "https://api.padelos.co/customers"
SITE = "https://player.padelos.co"
_s = requests.Session()
_cache: dict = {}

TIME_KEYS = ("startTime", "start_time", "start", "time", "from", "startAt", "start_at",
             "slotStart", "timeFrom", "slotTime", "fromTime")
END_KEYS = ("endTime", "end_time", "end", "to", "endAt", "end_at", "timeTo", "toTime")
DUR_KEYS = ("duration", "durationMinutes", "minutes", "slotDuration", "durationInMinutes")
PRICE_KEYS = ("price", "amount", "cost", "totalPrice", "priceAmount", "rate", "fee")
COURT_NAME_KEYS = ("courtName", "court_name", "resourceName", "courtTitle")
COURT_ID_KEYS = ("courtId", "court_id", "resourceId", "resource_id")
LIST_KEYS = ("slots", "timeslots", "timeSlots", "availability", "times", "availableSlots",
             "durations", "courts", "items")


def _headers(company_id, club_ids=None):
    h = {"User-Agent": BROWSER_UA, "Accept": "application/json, text/plain, */*",
         "Content-Type": "application/json", "Origin": SITE, "Referer": SITE + "/",
         "accept-language": "en", "version": "2.4", "x-clubos-channel": "CLUBOS-WEB",
         "x-clubos-domain": "PADELOSCO", "x-clubos-company": str(company_id),
         "x-client-route": f"/company/{company_id}/bookings"}
    if club_ids:
        h["x-clubos-club-info"] = ",".join(str(c) for c in club_ids)
    return h


def _request(method, url, company_id, club_ids=None, body=None):
    for attempt in range(3):
        try:
            r = _s.request(method, url, headers=_headers(company_id, club_ids),
                           data=json.dumps(body) if body is not None else None, timeout=40)
            _time.sleep(1.0)
            if r.status_code == 200:
                return r.json()
            err = f"HTTP {r.status_code} {' '.join(r.text[:160].split())}"
        except requests.RequestException as e:
            err = f"{type(e).__name__}: {e}"
        _time.sleep(3 * (attempt + 1))
    raise FetchError(f"{url} -> {err}")


def company_clubs(company_id):
    key = ("clubs", company_id)
    if key not in _cache:
        d = _request("GET", f"{API}/fetch-company-clubs/{company_id}", company_id)
        rows = (d.get("data") or {}).get("rows") if isinstance(d.get("data"), dict) else d.get("data")
        _cache[key] = [{"id": str(r["id"]), "name": r.get("name", "")} for r in rows or []]
    return _cache[key]


def search(company_id, d: date, club_ids):
    key = ("search", company_id, d.isoformat())
    if key not in _cache:
        body = {"date": d.isoformat(), "sport": "padel", "courtType": "", "courtSize": "",
                "courtTurf": "", "courtFeature": "", "searchTerm": "", "limit": "", "offset": "",
                "type": ""}
        _cache[key] = _request("POST", f"{API}/searchByDate", company_id, club_ids, body)
    return _cache[key]


def club_entry(resp, club_id):
    for c in (resp or {}).get("data") or []:
        if str(c.get("id")) == str(club_id):
            return c
    return None


def resolve(club: dict, resolved: dict) -> dict:
    info = resolved.get(club["key"]) or {}
    if info.get("club_id") and info.get("club_ids"):
        info.setdefault("courts", {})
        for n in list(info["courts"]):     # drop placeholders and anything now excluded
            if n == "Court (unnamed)" or n in (info.get("excluded") or []) or _excluded(club, n, ""):
                info["courts"].pop(n)
        return info
    clubs = company_clubs(club["company_id"])
    want = club["venue_match"].lower()
    match = [c for c in clubs if want in c["name"].lower()]
    if not match:
        raise RuntimeError(f"No PadelOS club matching '{club['venue_match']}'. Found: "
                           + ", ".join(c["name"] for c in clubs))
    info.update(club_id=match[0]["id"], club_name=match[0]["name"],
                club_ids=[c["id"] for c in clubs])
    info.setdefault("courts", {})
    resolved[club["key"]] = info
    return info


# ---------- reading slots (tolerant of a few possible layouts) ----------

def _parse_time(v, d: date, zone):
    """Return minutes after local midnight on d, or None."""
    if v is None:
        return None
    if isinstance(v, (int, float)) and 0 <= v < 1440:
        return int(v)
    s = str(v).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}T", s):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=zone)
        dt = dt.astimezone(zone)
        if dt.date() != d:
            return None
        return dt.hour * 60 + dt.minute
    m = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp][Mm])?$", s)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        h = h % 12 + (12 if m.group(3).lower() == "pm" else 0)
    return h * 60 + mi


def _court_of(d: dict, inherited):
    if isinstance(d.get("court"), dict):
        c = d["court"]
        return str(c.get("name") or c.get("title") or c.get("id"))
    if isinstance(d.get("court"), str):
        return d["court"]
    for k in COURT_NAME_KEYS:
        if d.get(k):
            return str(d[k])
    for k in COURT_ID_KEYS:
        if d.get(k) is not None:
            return inherited or f"Court {d[k]}"
    return inherited


def extract_slots(availability, d: date, zone):
    """[(court, start_min, duration_min, price)] from PadelOS availability.

    Real layout (Sep 2026): [{"duration": "60", "slots": [{"startTime": "08:00", "endTime": "09:00",
    "date": "YYYY-MM-DD", "courts": [{"id", "name", "price", "courtSize", ...}, ...]}]}, ...]
    Returns court dicts' names; singles/excluded courts are filtered by the caller."""
    out = []
    if isinstance(availability, list) and availability and all(
            isinstance(g, dict) and isinstance(g.get("slots"), list) for g in availability):
        for g in availability:
            for sl in g["slots"]:
                if sl.get("date") and sl["date"] != d.isoformat():
                    continue
                start = _parse_time(sl.get("startTime"), d, zone)
                end = _parse_time(sl.get("endTime"), d, zone)
                if start is None:
                    continue
                length = (end - start) if end and end > start else int(g.get("duration") or 60)
                for c in sl.get("courts") or []:
                    out.append(({"name": c.get("name") or f"Court {c.get('id')}",
                                 "size": (c.get("courtSize") or "").lower()},
                                start, length, parse_price(c.get("price"))))
        return out
    return _generic_slots(availability, d, zone)


def _generic_slots(availability, d: date, zone):
    out = []

    def walk(obj, court, parent_dur=None):
        if isinstance(obj, list):
            for x in obj:
                walk(x, court, parent_dur)
            return
        if not isinstance(obj, dict):
            return
        c = _court_of(obj, court)
        # a court object: has a name and a list of slots underneath
        if c == court and obj.get("name") and any(isinstance(obj.get(k), list) for k in LIST_KEYS) \
                and not any(k in obj for k in TIME_KEYS):
            c = str(obj["name"])
        dur = next((obj[k] for k in DUR_KEYS if isinstance(obj.get(k), (int, float, str))
                    and str(obj.get(k)).isdigit()), parent_dur)
        tkey = next((k for k in TIME_KEYS if k in obj), None)
        if tkey:
            start = _parse_time(obj[tkey], d, zone)
            opts = next((obj[k] for k in ("durations", "prices", "options", "slotOptions")
                         if isinstance(obj.get(k), list) and obj[k] and isinstance(obj[k][0], dict)), None)
            if start is not None and opts:
                for o in opts:
                    od = next((o[k] for k in DUR_KEYS if str(o.get(k, "")).isdigit()), None)
                    op = next((parse_price(o[k]) for k in PRICE_KEYS if o.get(k) not in (None, "")), None)
                    out.append((c, start, int(od or dur or 60), op))
                return
            if start is not None:
                ekey = next((k for k in END_KEYS if k in obj), None)
                end = _parse_time(obj[ekey], d, zone) if ekey else None
                length = (end - start) if end and end > start else int(dur or 60)
                price = next((parse_price(obj[k]) for k in PRICE_KEYS if obj.get(k) not in (None, "")), None)
                out.append((c, start, length, price))
        for k, v in obj.items():
            if isinstance(v, (list, dict)) and k not in ("company", "theme", "addresses", "palette"):
                walk(v, c, dur)

    walk(availability, None)
    return [({"name": c, "size": ""} if c else None, st, ln, pr) for c, st, ln, pr in out]


def _excluded(club, name, size):
    if size == "single" and not club.get("include_singles"):
        return True
    return any(e.lower() in (name or "").lower() for e in club.get("exclude_courts") or [])


def free_blocks(club, info, d: date, zone, block: int, mode=None):
    resp = search(club["company_id"], d, info["club_ids"])
    entry = club_entry(resp, info["club_id"])
    if entry is None:
        raise RuntimeError(f"Club {info['club_id']} missing from PadelOS response")
    avail = entry.get("availability") or []
    slots = extract_slots(avail, d, zone)
    if avail and not slots:
        raise RuntimeError("PadelOS availability format not recognised; sample: "
                           + json.dumps(avail)[:700])
    out: dict[str, dict[str, float | None]] = {}
    best: dict[tuple, int] = {}
    for court, start, length, price in slots:
        if court is None:
            raise RuntimeError("PadelOS slots have no court information; sample: " + json.dumps(avail)[:500])
        name, size = court["name"], court["size"]
        if _excluded(club, name, size):
            info.setdefault("excluded", [])
            if name not in info["excluded"]:
                info["excluded"].append(name)
            continue
        info["courts"].setdefault(name, name)
        rate = price / (length / block) if price is not None and length else None
        for m in range(start, min(start + length, 24 * 60), block):
            hm, key = min_to_hm(m), (name, min_to_hm(m))
            if key not in best or abs(length - 60) < abs(best[key] - 60):
                best[key] = length
                out.setdefault(name, {})[hm] = rate
    return out


def strip_bulk(resp):
    """Copy of a response without the large repeated company/T&C text, for saving."""
    r = json.loads(json.dumps(resp))
    for c in (r or {}).get("data") or []:
        c.pop("company", None)
        c.pop("theme", None)
        for k in ("description", "faq", "openMatchGuide", "aiIntroduction"):
            c.pop(k, None)
    return r
