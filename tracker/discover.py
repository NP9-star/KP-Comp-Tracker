"""Connection check. Run from the Actions tab (Run workflow -> mode: discover) and send the log to Claude."""
from __future__ import annotations

import collections
import json
from datetime import timedelta

from . import matchi, net, padelos, playtomic
from .collect import club_hours
from .common import DATA, load_config, min_to_hm, now_utc, tz, write_json


def main():
    cfg = load_config()
    zone = tz(cfg)
    block = int(cfg.get("block_minutes", 30))
    day = now_utc().astimezone(zone).date() + timedelta(days=1)
    resolved: dict = {}
    dbg = DATA / "debug"
    dbg.mkdir(parents=True, exist_ok=True)
    matchi.DEBUG["on"] = True
    pt = []
    for club in cfg["clubs"]:
        print("=" * 70)
        print(club["name"], f"({club['platform']})", "checking", day)
        try:
            if club["platform"] == "playtomic":
                info = playtomic.resolve(club, resolved)
                pt.append((club, info))
                print("tenant:", info["tenant_id"])
                print("club page:", "OK" if not info.get("page_error") else f"FAILED {info['page_error']}")
                print("hours from page:", info.get("hours"))
                print("courts listed on page:", info.get("page_courts") or "none read")
                print("court ids matched to names:", len(info.get("page_names") or {}))
                try:
                    _, _, _, _, _, html = playtomic._page_info(club)
                    (dbg / f"{club['key']}_club_page.html").write_text(html, encoding="utf-8")
                except Exception:  # noqa: BLE001
                    pass
                raw = playtomic.availability(club, info["tenant_id"], day)
                write_json(dbg / f"{club['key']}_availability.json", raw, compact=False)
                table = {}
                for r in raw or []:
                    rates = sorted(float(s["price"].split()[0]) / (int(s["duration"]) / 60)
                                   for s in r.get("slots") or [] if s.get("price"))
                    if rates:
                        table[r["resource_id"][:8]] = f"£{rates[len(rates) // 2]:.0f}/hr, {len(rates)} free slots"
                print(f"resources in response: {len(raw or [])}; per-court typical price:", table)
            elif club["platform"] == "padelos":
                info = padelos.resolve(club, resolved)
                print("club:", info["club_id"], info.get("club_name"), "| company clubs:", info["club_ids"])
                for i in range(3):
                    dd = day + timedelta(days=i)
                    resp = padelos.search(club["company_id"], dd, info["club_ids"])
                    write_json(dbg / f"padelos_search_{dd}.json", padelos.strip_bulk(resp), compact=False)
                    entry = padelos.club_entry(resp, info["club_id"]) or {}
                    avail = entry.get("availability") or []
                    print(f"  {dd}: {len(avail)} availability entries")
                    if avail:
                        print("     sample:", json.dumps(avail[0])[:900])
                    others = [c for c in resp.get("data") or [] if c.get("availability")]
                    if not avail and others and i == 0:
                        print(f"     sample from {others[0].get('name')}:",
                              json.dumps(others[0]["availability"][0])[:900])
                    try:
                        free = padelos.free_blocks(club, info, dd, zone, block)
                        print("     excluded:", info.get("excluded"), "| free blocks per court:", {c: len(v) for c, v in free.items()},
                              "| earliest/latest:", min((min(v) for v in free.values() if v), default=None),
                              max((max(v) for v in free.values() if v), default=None))
                    except Exception as ex:  # noqa: BLE001
                        print("     reader:", ex)
            else:
                info = matchi.resolve(club, resolved)
                print("facility id:", info["facility_id"], "| sport id:", info["sport"])
                html = matchi.schedule_html(club, info["facility_id"], day, info["sport"], None)
                cells = list(matchi.parse_cells(html))
                print("cells:", len(cells), "| classes:", dict(collections.Counter(c[4] for c in cells).most_common(5)))
                print("courts:", sorted({c[0] for c in cells}))
                print("sample:", [(c[0], min_to_hm(c[1]), "free" if c[3] else "taken") for c in cells[:4]])
        except Exception as ex:  # noqa: BLE001
            print("ERROR:", type(ex).__name__, str(ex)[:600])
    print("=" * 70)
    try:
        playtomic.detect_times(pt, resolved, zone, club_hours)
        print("Playtomic time format:", resolved.get("_playtomic"))
    except Exception as ex:  # noqa: BLE001
        print("Playtomic time format check failed:", ex)
    for club, info in pt:
        try:
            mode = playtomic.times_mode(club, resolved)
            free = playtomic.free_blocks(club, info, day, zone, block, mode)
            print(f"{club['name']}: courts tracked {sorted(info['courts'].values())}")
            print(f"   excluded {sorted(set(info.get('all_courts', {}).values()) - set(info['courts'].values()))}")
            print("   free blocks per court:", {c: len(v) for c, v in free.items()},
                  "| earliest/latest free (local):",
                  min((min(v) for v in free.values() if v), default=None),
                  max((max(v) for v in free.values() if v), default=None))
        except Exception as ex:  # noqa: BLE001
            print(club["name"], "ERROR:", ex)
    if net.used_browser:
        print("Headless-browser fallback was needed for:", ", ".join(sorted(net.used_browser)))
    net.close()
    print("Raw responses saved in data/debug/ (download 'debug' from the run summary page).")


if __name__ == "__main__":
    main()
