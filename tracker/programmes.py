"""Club programmes: classes, courses, tournaments and club events (club-organised court use),
plus UK Padel open matches. Read every few hours and kept in data/programmes/<club>.json.

Playtomic: read from the public club page and its "See more activities" pages
  /clubs/<slug>/academy/public-classes and /clubs/<slug>/competitions/tournaments.
  Cards give day, time, duration, places filled/total and price per player (no court).
PadelOS (UK Padel): trainings, tournaments, club events and open matches feeds.
No personal data is stored: only times, places, prices and (for open matches) the court.
"""
from __future__ import annotations

import html as htmlmod
import json
import math
import re
from datetime import date, datetime, timedelta

from . import net
from .common import DATA, read_json, write_json

CARD_LINK = re.compile(r'href="/activities/(public-classes|tournaments|courses|leagues|classes)/([0-9a-f-]{36})"')
DATE_RE = re.compile(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+([A-Z][a-z]+)\s+(\d{1,2})\s*\|\s*(\d{1,2}):(\d{2})h")
MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August",
                                       "September", "October", "November", "December"], 1)}
KIND = {"public-classes": "class", "classes": "class", "courses": "course", "tournaments": "tournament",
        "leagues": "league"}


# ------------------------------------------------------------------ helpers

def est_courts(kind, capacity):
    """Courts an activity is likely to use: 4 players a court for competitions and big
    groups; a class of up to 6 is one court with a coach."""
    cap = int(capacity or 0)
    if kind in ("class", "course", "training"):
        return 1 if cap <= 6 else math.ceil(cap / 4)
    if cap:
        return max(1, math.ceil(cap / 4))
    return 2


def _text(fragment):
    t = re.sub(r"<[^>]+>", "\n", fragment)
    t = htmlmod.unescape(t)
    return [l.strip() for l in t.split("\n") if l.strip()]


def _year_for(month, day, today):
    y = today.year
    d = date(y, month, day)
    if d < today - timedelta(days=120):
        d = date(y + 1, month, day)
    return d


def parse_playtomic_cards(page_html, today):
    """[{id, kind, name, start, minutes, filled, capacity, price}] from Playtomic activity cards."""
    out, last = [], 0
    for m in CARD_LINK.finditer(page_html):
        chunk = page_html[last:m.start()]
        last = m.end()
        lines = _text(chunk)
        joined = " ".join(lines)
        dms = list(DATE_RE.finditer(joined))
        dm = dms[-1] if dms else None       # the card's own date is the last one before its link
        if not dm or dm.group(2) not in MONTHS:
            continue
        d = _year_for(MONTHS[dm.group(2)], int(dm.group(3)), today)
        start = datetime(d.year, d.month, d.day, int(dm.group(4)), int(dm.group(5)))
        tail = joined[dm.start():]
        mins = re.search(r"\b(\d{2,3})\s*min\b", tail)
        fill = re.findall(r"(?<![\d.–-])(\d{1,3})\s*/\s*(\d{1,3})(?![\d.])", tail)
        price = re.findall(r"£\s*(\d+(?:\.\d{1,2})?)", tail)
        name_m = re.search(r'aria-label="([^"]{1,140})"', page_html[m.start() - 400:m.end()])
        name = htmlmod.unescape(name_m.group(1)) if name_m else ""
        if not name:  # the line after the date line
            for i, l in enumerate(lines):
                if DATE_RE.search(l) and i + 1 < len(lines):
                    name = lines[i + 1]
                    break
        out.append({"id": m.group(2), "kind": KIND.get(m.group(1), m.group(1)), "name": name[:120],
                    "start": start.isoformat(timespec="minutes"), "minutes": int(mins.group(1)) if mins else 60,
                    "filled": int(fill[-1][0]) if fill else None, "capacity": int(fill[-1][1]) if fill else None,
                    "price": float(price[-1]) if price else None})
    return out


# ------------------------------------------------------------------ Playtomic

def fetch_playtomic(club, today):
    base = f"https://playtomic.com/clubs/{club['slug']}"
    found = {}
    for path in ("", "/academy/public-classes", "/competitions/tournaments"):
        try:
            page = net.get(base + path, accept="text/html,application/xhtml+xml", allow_browser=False)
        except net.FetchError:
            continue
        for a in parse_playtomic_cards(page, today):
            found[a["id"]] = a
    return list(found.values())


# ------------------------------------------------------------------ PadelOS (UK Padel)

def _padelos_rows(club, info, url, horizon: date | None = None, max_pages=30):
    """Read pages until the feed runs out or reaches the horizon (feeds list from term start)."""
    from . import padelos
    rows = []
    for page in range(1, max_pages + 1):
        d = padelos._request("GET", url.format(club=info["club_id"], page=page), club["company_id"], info.get("club_ids"))
        data = d.get("data") or {}
        batch = (data.get("rows") if isinstance(data, dict) else data) or []
        rows += batch
        if not (isinstance(data, dict) and data.get("hasMore")) or not batch:
            break
        last = max((str(r.get("startDate") or r.get("date") or "") for r in batch), default="")
        if horizon and last > horizon.isoformat():
            break
    return rows


SAMPLES: list = []   # a few term-course rows (no personal data), printed by discover


def _sessions(r, kind):
    """Dates a row takes place on. A course with an end date after its start date and a weekly
    (or daily) recurrence is expanded into its individual sessions."""
    sd = r.get("startDate") or r.get("date")
    ed = r.get("endDate") or sd
    if kind != "training" or not ed or ed <= sd:
        return [sd]
    step = 1 if str(r.get("recurrType", "")).lower() == "daily" else 7
    d0, d1 = date.fromisoformat(sd), date.fromisoformat(ed)
    out, d = [], d0
    while d <= d1 and len(out) < 60:
        out.append(d.isoformat())
        d += timedelta(days=step)
    n = int(r.get("totalSessions") or 0)
    return out[:n] if n and n < len(out) else out


def fetch_padelos(club, info, today: date | None = None):
    from .padelos import API
    today = today or date.today()
    horizon = today + timedelta(days=14)
    out = []
    feeds = [
        ("training", API + "/trainings/listing?clubId={club}&trainerId=&day=&startTime=&endTime=&limit=50&availability=&sport=Padel&categoryNames=&page={page}"),
        ("tournament", API + "/tournament/listing?clubId={club}&limit=50&sport=Padel&format=&matchType=&category=&availability=&page={page}"),
        ("event", API + "/club/eventsBooking?eventType=active&clubId={club}&status=Published&type=&limit=50&availability=&page={page}&sport=Padel"),
        ("open_match", "https://api.padelos.co/V2/customers/open-match/listing?sport=Padel&page={page}&limit=50&clubId={club}&gender=&date=&availability="),
    ]
    for kind, url in feeds:
        rows = None
        for u in (url, re.sub(r"availability=(&|$)", r"availability=available\1", url)):
            try:
                rows = _padelos_rows(club, info, u, horizon)
                break
            except Exception:  # noqa: BLE001 - try the other variant, then give up on this feed
                continue
        if rows is None:
            continue
        for r in rows:
            if str(r.get("clubId") or (r.get("club") or {}).get("id") or (r.get("club") or {}).get("clubId")) != str(info["club_id"]):
                continue
            st, en = (r.get("startTime") or "")[:5], (r.get("endTime") or "")[:5]
            if not (st and en) or not (r.get("startDate") or r.get("date")):
                continue
            dates = _sessions(r, kind)
            n_sess = len(dates)
            if kind == "open_match":
                filled = len(r.get("participants") or [])
                cap = 4 if (r.get("courtSize") or "Double").lower().startswith("d") else 2
                price = r.get("pricePerSlot")
                court = (r.get("court") or {}).get("name")
            else:
                filled = r.get("participantCount") if r.get("participantCount") is not None else r.get("activeParticipants")
                cap = r.get("maxParticipants")
                if cap is None and r.get("remainingSlots") not in (None, ""):
                    cap = int(r["remainingSlots"]) + int(filled or 0)
                price = r.get("participantPrice") or r.get("price")
                if price not in (None, "") and n_sess > 1:
                    price = float(price) / n_sess          # term price spread across its sessions
                court = None
            if kind == "training" and n_sess > 1 and len(SAMPLES) < 3:
                SAMPLES.append({k: r.get(k) for k in ("name", "type", "recurrType", "startDate", "endDate",
                                                     "startTime", "endTime", "totalSessions", "participantPrice",
                                                     "maxParticipants", "participantCount")})
            for day in dates:
                if day < (today - timedelta(days=3)).isoformat() or day > horizon.isoformat():
                    continue
                s_ = datetime.fromisoformat(f"{day}T{st}")
                e_ = datetime.fromisoformat(f"{day}T{en}")
                minutes = int((e_ - s_).total_seconds() // 60) or 60
                out.append({"id": f"{kind}-{r.get('id')}" + (f"-{day}" if n_sess > 1 else ""), "kind": kind,
                            "name": (r.get("name") or "Open match")[:120],
                            "start": s_.isoformat(timespec="minutes"), "minutes": minutes,
                            "filled": int(filled) if filled not in (None, "") else None,
                            "capacity": int(cap) if cap not in (None, "") else None,
                            "price": round(float(price), 2) if price not in (None, "") else None, "court": court})
    return out


# ------------------------------------------------------------------ store

def update(cfg, resolved, now_local):
    """Refresh every club's programme list (called from collect every few runs)."""
    today = now_local.date()
    for club in cfg["clubs"]:
        key = club["key"]
        try:
            if club["platform"] == "playtomic":
                items = fetch_playtomic(club, today)
            elif club["platform"] == "padelos" and resolved.get(key, {}).get("club_id"):
                items = fetch_padelos(club, resolved[key], today)
            else:
                continue
        except Exception as e:  # noqa: BLE001
            print(f"WARN programmes not updated for {club['name']}: {e}")
            continue
        path = DATA / "programmes" / f"{key}.json"
        store = read_json(path, {})
        stamp = now_local.isoformat(timespec="minutes")
        for a in items:
            if a["start"] < (now_local - timedelta(hours=1)).isoformat():
                continue  # only update activities that haven't started: keeps the last fill seen before play
            old = store.get(a["id"], {})
            a["first_seen"] = old.get("first_seen", stamp)
            a["last_seen"] = stamp
            store[a["id"]] = {**old, **a}
        cutoff = (today - timedelta(days=400)).isoformat()
        store = {k: v for k, v in store.items() if v.get("start", "") >= cutoff}
        write_json(path, store)
        print(f"     programmes {club['name']}: {len(items)} listed, {len(store)} on file")
