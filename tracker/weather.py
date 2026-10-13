"""Daily and hourly rainfall and temperature for the area (Open-Meteo, free, no key).
Stored in data/weather.json as {date: {rain_mm, rain_hours, tmax_c, hourly_mm: {HH: mm}}}."""
from __future__ import annotations

import requests

from .common import DATA, read_json, write_json

URL = "https://api.open-meteo.com/v1/forecast"


def update(cfg, area: str | None = None):
    """Main area (config 'weather') by default; a group's own area from 'group_weather' if given."""
    if area:
        w = (cfg.get("group_weather") or {}).get(area) or {}
        path = DATA / f"weather_{area}.json"
    else:
        w = cfg.get("weather") or {}
        path = DATA / "weather.json"
    if not w.get("lat"):
        return read_json(path, {})
    store = read_json(path, {})
    try:
        r = requests.get(URL, params={
            "latitude": w["lat"], "longitude": w["lon"], "timezone": cfg.get("timezone", "Europe/London"),
            "hourly": "precipitation", "daily": "precipitation_sum,temperature_2m_max",
            "past_days": 14 if store else 92, "forecast_days": 1}, timeout=30)   # back-fill a new area
        r.raise_for_status()
        d = r.json()
    except Exception as e:  # noqa: BLE001 - weather is optional
        print(f"WARN weather not updated: {e}")
        return store
    hourly = {}
    for t, mm in zip(d["hourly"]["time"], d["hourly"]["precipitation"]):
        hourly.setdefault(t[:10], {})[t[11:13]] = mm
    for day, mm, tmax in zip(d["daily"]["time"], d["daily"]["precipitation_sum"], d["daily"]["temperature_2m_max"]):
        h = hourly.get(day, {})
        store[day] = {"rain_mm": mm, "tmax_c": tmax, "hourly_mm": h,
                      "rain_hours": sum(1 for hh, v in h.items() if v and v >= 0.2 and 7 <= int(hh) < 22)}
    write_json(path, store)
    return store


def by_group(cfg):
    """{group: weather store}; groups without their own area use the main one."""
    main = update(cfg)
    out = {g: update(cfg, g) for g in (cfg.get("group_weather") or {})}
    return out, main
