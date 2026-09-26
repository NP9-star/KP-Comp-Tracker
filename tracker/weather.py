"""Daily and hourly rainfall and temperature for the area (Open-Meteo, free, no key).
Stored in data/weather.json as {date: {rain_mm, rain_hours, tmax_c, hourly_mm: {HH: mm}}}."""
from __future__ import annotations

import requests

from .common import DATA, read_json, write_json

URL = "https://api.open-meteo.com/v1/forecast"


def update(cfg):
    w = cfg.get("weather") or {}
    if not w.get("lat"):
        return read_json(DATA / "weather.json", {})
    store = read_json(DATA / "weather.json", {})
    try:
        r = requests.get(URL, params={
            "latitude": w["lat"], "longitude": w["lon"], "timezone": cfg.get("timezone", "Europe/London"),
            "hourly": "precipitation", "daily": "precipitation_sum,temperature_2m_max",
            "past_days": 14, "forecast_days": 1}, timeout=30)
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
    write_json(DATA / "weather.json", store)
    return store
