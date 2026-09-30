"""Discovery for club programming: open each club's Open matches / Events / Academy tabs in a
headless browser and record the data those tabs load, so the reader can be built against the
real format. Output goes to data/debug/activities/ (downloadable from the discover run).
"""
from __future__ import annotations

import json
import re

from .common import DATA

NOISE = re.compile(r"google|gstatic|facebook|doubleclick|sentry|cookiebot|hotjar|segment|amplitude|"
                   r"datadog|intercom|clarity|tiktok|linkedin|mixpanel|braze|onetrust|branch\.io|"
                   r"\.(png|jpe?g|webp|svg|gif|woff2?|ttf|css)(\?|$)", re.I)
PLAYTOMIC_TABS = {
    "open_matches": ["Open matches", "Open Matches", "Matches"],
    "events": ["Events", "Tournaments", "Competitions"],
    "academy": ["Academy", "Classes", "Lessons", "Courses"],
}
PADELOS_TABS = {
    "open_matches": ["Open Matches", "Open matches", "Matches"],
    "events": ["Tournaments", "Events", "Socials", "Leagues"],
    "academy": ["Coaching", "Classes", "Academy", "Lessons", "Programmes"],
}
# a few clubs is enough to learn each platform's format
DEFAULT_PROBE = ["padel-maidenhead", "psc-earls-court", "rulo-chesham", "padelhub-slough",
                 "ukpadel-holmer-green"]


def _recorder(page, records):
    def on_response(resp):
        req = resp.request
        if req.resource_type not in ("xhr", "fetch", "document") or NOISE.search(resp.url):
            return
        try:
            body = resp.text()
        except Exception:  # noqa: BLE001
            body = ""
        records.append({"url": resp.url, "method": req.method, "status": resp.status,
                        "type": req.resource_type, "content_type": resp.headers.get("content-type", ""),
                        "post_data": req.post_data, "body": body[:300000]})
    page.on("response", on_response)
    return on_response


def _dismiss_cookies(page):
    for label in ("Accept all", "Accept All", "Allow all", "Accept", "I agree", "Got it", "OK"):
        try:
            b = page.get_by_role("button", name=label, exact=True).first
            if b.count() and b.is_visible():
                b.click(timeout=2000)
                page.wait_for_timeout(800)
                return
        except Exception:  # noqa: BLE001
            continue


def _click_tab(page, labels):
    for label in labels:
        for how in ("tab", "link", "button", "text"):
            try:
                loc = page.get_by_text(label, exact=True).first if how == "text" else \
                    page.get_by_role(how, name=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=4000)
                    return label
            except Exception:  # noqa: BLE001
                continue
    return None


def _capture(url, tabs, key, out_dir, log):
    from playwright.sync_api import sync_playwright
    from .net import BROWSER_UA
    out_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(user_agent=BROWSER_UA, locale="en-GB", timezone_id="Europe/London",
                                  viewport={"width": 1400, "height": 1100})
        page = ctx.new_page()
        for tab, labels in tabs.items():
            records = []
            handler = _recorder(page, records)
            try:
                page.goto(url, wait_until="networkidle", timeout=60000)
            except Exception:  # noqa: BLE001
                pass
            _dismiss_cookies(page)
            clicked = _click_tab(page, labels)
            page.wait_for_timeout(5000)
            for _ in range(3):                      # scroll to trigger lazy loading
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(1200)
            base = out_dir / f"{key}_{tab}"
            try:
                page.screenshot(path=str(base) + ".png", full_page=True)
                (base.with_suffix(".txt")).write_text(page.inner_text("body"), encoding="utf-8")
                (base.with_suffix(".html")).write_text(page.content(), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
            (base.with_name(base.name + "_network.json")).write_text(json.dumps(records, indent=1), encoding="utf-8")
            page.remove_listener("response", handler)
            data = [r for r in records if r["type"] != "document" and r["status"] == 200]
            log.append(f"  {tab}: clicked {clicked!r} -> {page.url}")
            for r in sorted(data, key=lambda r: -len(r["body"]))[:6]:
                log.append(f"     {r['method']} {r['url'][:130]}  [{r['content_type'][:30]}, {len(r['body'])} chars]")
        browser.close()


def discover(cfg, clubs_by_key, log):
    probe = cfg.get("activity_probe") or DEFAULT_PROBE
    out = DATA / "debug" / "activities"
    for key in probe:
        club = clubs_by_key.get(key)
        if not club:
            continue
        log.append(f"{club['name']} ({club['platform']})")
        try:
            if club["platform"] == "playtomic":
                _capture(f"https://playtomic.com/clubs/{club['slug']}", PLAYTOMIC_TABS, key, out, log)
            elif club["platform"] == "padelos":
                _capture(f"https://player.padelos.co/company/{club['company_id']}", PADELOS_TABS, key, out, log)
        except Exception as e:  # noqa: BLE001
            log.append(f"  ERROR {type(e).__name__}: {e}")
