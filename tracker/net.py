"""HTTP with browser-like headers, and a headless-browser fallback if a site refuses plain requests."""
from __future__ import annotations

import atexit
import json
import time

import requests

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
BASE_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept-Language": "en-GB,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="140", "Not?A_Brand";v="24", "Google Chrome";v="140"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}
PAUSE = 0.8          # seconds between requests, to be gentle
_session = requests.Session()
_session.headers.update(BASE_HEADERS)
_browser = {"pw": None, "browser": None, "ctx": None, "warmed": set()}
used_browser = set()  # hosts where the browser fallback was needed (reported in logs)


class FetchError(RuntimeError):
    pass


def _describe(status, headers, body: str) -> str:
    server = (headers or {}).get("server") or (headers or {}).get("Server") or "?"
    snippet = " ".join((body or "")[:160].split())
    return f"HTTP {status} (server: {server}) {snippet}"


def _browser_get(url: str, referer: str | None) -> tuple[int, str]:
    from playwright.sync_api import sync_playwright  # installed in the workflow
    if _browser["ctx"] is None:
        _browser["pw"] = sync_playwright().start()
        _browser["browser"] = _browser["pw"].chromium.launch()
        _browser["ctx"] = _browser["browser"].new_context(user_agent=BROWSER_UA, locale="en-GB")
        atexit.register(close)
    page = _browser["ctx"].new_page()
    try:
        if referer and referer not in _browser["warmed"]:
            page.goto(referer, wait_until="domcontentloaded", timeout=45000)
            _browser["warmed"].add(referer)
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        return (resp.status if resp else 0), (resp.text() if resp else "")
    finally:
        page.close()


def close():
    try:
        if _browser["browser"]:
            _browser["browser"].close()
        if _browser["pw"]:
            _browser["pw"].stop()
    except Exception:
        pass
    _browser.update(pw=None, browser=None, ctx=None)


def get(url: str, params: dict | None = None, *, referer: str | None = None,
        accept: str = "*/*", want: str = "text", allow_browser: bool = True):
    """GET a URL. want='json' returns parsed JSON, 'text' returns the body."""
    headers = {"Accept": accept}
    if referer:
        headers["Referer"] = referer
    full = requests.Request("GET", url, params=params).prepare().url
    err = None
    for attempt in range(2):
        try:
            r = _session.get(full, headers=headers, timeout=30)
            time.sleep(PAUSE)
            if r.status_code == 200:
                return r.json() if want == "json" else r.text
            err = _describe(r.status_code, r.headers, r.text)
            if r.status_code not in (429, 500, 502, 503, 504):
                break
        except requests.RequestException as e:
            err = f"{type(e).__name__}: {e}"
        time.sleep(3 * (attempt + 1))
    if allow_browser:
        try:
            status, body = _browser_get(full, referer)
            time.sleep(PAUSE)
            if status == 200:
                used_browser.add(requests.utils.urlparse(full).netloc)
                if want == "json":
                    return json.loads(body)
                return body
            err = f"{err} | browser: {_describe(status, {}, body)}"
        except ImportError:
            err = f"{err} | browser fallback not installed"
        except Exception as e:  # noqa: BLE001
            err = f"{err} | browser: {type(e).__name__}: {e}"
    raise FetchError(f"{url} -> {err}")
