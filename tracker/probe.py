"""One-off: open a page in the headless browser and record where its booking widget loads from.
Writes data/probe.json (iframe addresses, data requests, visible text). Temporary."""
from __future__ import annotations

from .common import DATA, write_json
from .net import BROWSER_UA

SKIP = ("google", "gstatic", "facebook", "doubleclick", "hotjar", "fonts.", "cloudflareinsights",
        ".css", ".woff", ".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp", ".ico")


def run(urls):
    from playwright.sync_api import sync_playwright
    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(user_agent=BROWSER_UA, locale="en-GB", timezone_id="Europe/London",
                            viewport={"width": 1280, "height": 1600})
        for url in urls:
            reqs, rpc, pg = [], [], ctx.new_page()
            pg.on("request", lambda r: reqs.append(f"{r.method} {r.resource_type} {r.url}"[:400]))

            def on_resp(resp):
                if "cloudfunctions.net" in resp.url or "nettla" in resp.url and resp.request.resource_type == "fetch":
                    try:
                        body = resp.text()
                    except Exception:  # noqa: BLE001
                        body = ""
                    rpc.append({"url": resp.url, "status": resp.status, "req_headers": {k: v for k, v in resp.request.headers.items() if k.lower() not in ("cookie",)},
                                "post": (resp.request.post_data or "")[:3000], "body": body[:20000]})
            pg.on("response", on_resp)
            try:
                pg.goto(url, wait_until="networkidle", timeout=60000)
                pg.wait_for_timeout(6000)
                frames = [{"url": f.url[:400], "text": (f.locator("body").inner_text(timeout=5000) or "")[:3000]}
                          for f in pg.frames if f != pg.main_frame]
                text = pg.locator("body").inner_text(timeout=5000)[:4000]
                links = pg.eval_on_selector_all("a[href]", "as => as.map(a => a.href)")
                widgets = pg.evaluate("() => [...document.querySelectorAll('*')].filter(e => e.tagName.toLowerCase().startsWith('nettla')).map(e => e.outerHTML.slice(0, 600))")
            except Exception as e:  # noqa: BLE001
                frames, text, links, widgets = [], f"ERROR {type(e).__name__}: {e}", [], []
            out[url] = {"requests": [r for r in reqs if not any(s in r.lower() for s in SKIP)][:150],
                        "frames": frames, "text": text, "widgets": widgets, "rpc": rpc,
                        "links": sorted({l for l in links if not any(s in l.lower() for s in SKIP)})[:150]}
            pg.close()
        b.close()
    write_json(DATA / "probe.json", out, compact=False)
