"""PadelOS adapter (UK Padel's booking system, player.padelos.co).

Step 1 (now): discover opens the public bookings page in a headless browser and records
the data it loads, so the reader can be written against the real format.
Step 2: free_blocks() reads that data directly.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from .common import DATA

SITE = "https://player.padelos.co"


def bookings_url(club, d: date | None = None):
    return f"{SITE}/company/{club['company_id']}/bookings"


def capture(club, out_dir=None):
    """Load the public bookings page like a visitor and save every data response it makes."""
    from playwright.sync_api import sync_playwright
    from .net import BROWSER_UA
    out_dir = out_dir or DATA / "debug"
    out_dir.mkdir(parents=True, exist_ok=True)
    records = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(user_agent=BROWSER_UA, locale="en-GB", timezone_id="Europe/London",
                                  viewport={"width": 1400, "height": 1000})
        page = ctx.new_page()

        def on_response(resp):
            req = resp.request
            if req.resource_type not in ("xhr", "fetch"):
                return
            try:
                body = resp.text()
            except Exception:  # noqa: BLE001
                body = ""
            hdrs = {k: v for k, v in req.headers.items() if k.lower() not in ("cookie",)}
            records.append({"url": resp.url, "method": req.method, "status": resp.status,
                            "request_headers": hdrs, "post_data": req.post_data,
                            "content_type": resp.headers.get("content-type", ""),
                            "body": body[:60000]})

        page.on("response", on_response)
        page.goto(bookings_url(club), wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(6000)
        page.screenshot(path=str(out_dir / f"{club['key']}_page.png"), full_page=True)
        (out_dir / f"{club['key']}_page_text.txt").write_text(page.inner_text("body"), encoding="utf-8")
        (out_dir / f"{club['key']}_page.html").write_text(page.content(), encoding="utf-8")
        # try moving to the next day so the date parameter shows up in the recorded requests
        for label in ("Tomorrow", "Next", "›", ">", "→"):
            try:
                btn = page.get_by_text(label, exact=True).first
                if btn.count():
                    btn.click(timeout=3000)
                    page.wait_for_timeout(4000)
                    break
            except Exception:  # noqa: BLE001
                continue
        browser.close()
    (out_dir / f"{club['key']}_network.json").write_text(json.dumps(records, indent=1), encoding="utf-8")
    return records


def resolve(club, resolved):
    raise RuntimeError("UK Padel (PadelOS) reader not built yet: run discover and send the debug zip to Claude")


def free_blocks(club, info, d, zone, block, mode=None):
    raise RuntimeError("UK Padel (PadelOS) reader not built yet")
