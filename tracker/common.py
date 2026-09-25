"""Shared helpers: config, time handling, opening hours, prices, JSON storage."""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOCS_DATA = ROOT / "docs" / "data"

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def tz(cfg) -> ZoneInfo:
    return ZoneInfo(cfg.get("timezone", "Europe/London"))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def read_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, obj, compact=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, separators=(",", ":"), sort_keys=True)
        else:
            json.dump(obj, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


# ---------- day ranges like "mon-fri", "sat-sun", "all", "sun" ----------

def expand_days(spec: str) -> list[int]:
    spec = spec.strip().lower()
    if spec == "all":
        return list(range(7))
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            ia, ib = DAY_NAMES.index(a[:3]), DAY_NAMES.index(b[:3])
            rng = range(ia, ib + 1) if ia <= ib else list(range(ia, 7)) + list(range(0, ib + 1))
            out.extend(rng)
        else:
            out.append(DAY_NAMES.index(part[:3]))
    return sorted(set(out))


def hm_to_min(s: str) -> int:
    h, m = s.strip().split(":")[:2]
    return int(h) * 60 + int(m)


def min_to_hm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def parse_range(s: str) -> tuple[int, int]:
    a, b = s.split("-")
    start, end = hm_to_min(a), hm_to_min(b)
    if end <= start:          # "06:00-00:00" means until midnight
        end += 24 * 60
    return start, min(end, 24 * 60)


def hours_by_weekday(hours: dict | None) -> dict[int, tuple[int, int]]:
    """{'mon-fri': '07:00-22:00', ...} -> {0: (420, 1320), ...}"""
    out: dict[int, tuple[int, int]] = {}
    if not hours:
        return out
    for spec, rng in hours.items():
        for d in expand_days(spec):
            out[d] = parse_range(rng)
    return out


def blocks_for_day(hours: dict[int, tuple[int, int]], d: date, block: int) -> list[str]:
    if d.weekday() not in hours:
        return []
    start, end = hours[d.weekday()]
    return [min_to_hm(m) for m in range(start, end - block + 1, block)]


def is_peak(cfg, d: date, hm: str) -> bool:
    key = "weekend" if d.weekday() >= 5 else "weekday"
    start, end = parse_range(cfg["peak"][key])
    return start <= hm_to_min(hm) < end


def config_price_per_hour(club: dict, d: date, hm: str) -> float | None:
    for row in club.get("prices") or []:
        if row.get("per_hour") in (None, ""):
            continue
        if d.weekday() not in expand_days(row["days"]):
            continue
        start, end = parse_range(f'{row["from"]}-{row["to"]}')
        if start <= hm_to_min(hm) < end:
            return float(row["per_hour"])
    return None


PRICE_RE = re.compile(r"([\d.,]+)")


def parse_price(s) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = PRICE_RE.search(str(s))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def local_day_bounds_utc(d: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time(0, 0), tzinfo=zone)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def block_start(d: date, hm: str, zone: ZoneInfo) -> datetime:
    m = hm_to_min(hm)
    return datetime.combine(d, time(0, 0), tzinfo=zone) + timedelta(minutes=m)
