# Local padel market tracker

Measures occupancy and estimated court-hire revenue at four local padel clubs from their
public booking availability, and shows it on a dashboard that updates every 30 minutes.

| Club | Platform |
|---|---|
| Padel Maidenhead | Playtomic |
| PADELHUB SL2 Slough | Playtomic |
| UK Padel Holmer Green | MATCHi |
| UK Padel Stoke Poges | MATCHi |

## Set up (about 15 minutes, no coding)

1. **Create the repository.** On github.com: New repository, give it a neutral name such as `padel-market-tracker`.
   Choose Public: GitHub Pages and Actions minutes are free. Anyone can see the code and the
   collected data, so nothing in this repository identifies your own club or plans. The
   dashboard is not indexed by search engines.
2. **Upload the files.** "Add file → Upload files", drag in everything from the zip.
   The `.github` folder is hidden on Mac/Windows and often gets skipped. Check afterwards that
   `.github/workflows/track.yml` exists; if not, use "Add file → Create new file", type that
   path as the name and paste the file's contents in.
3. **Let the workflow save data.** Settings → Actions → General → Workflow permissions →
   "Read and write permissions" → Save.
4. **Run the connection check.** Actions tab → "Track competitor availability" → Run workflow →
   mode `discover`. Open the finished run, click the "Discover" step, copy the log and send it
   to Claude. This confirms each club is found and the times and courts look right.
5. **Start tracking.** Run workflow again with mode `track`. After that it runs by itself
   every 30 minutes.
6. **Turn on the dashboard.** Settings → Pages → Source "Deploy from a branch" → branch `main`,
   folder `/docs` → Save. The address appears at the top of that page after a minute or two.
   Add `?demo=1` to the address to preview the layout with invented numbers.
7. **Show your own plan privately.** Bookmark the dashboard address with `#plan=40-55` on the end
   (your year-one and year-three occupancy). The band is drawn in your browser only; the part
   after `#` is never sent to GitHub or stored in the repository.

## Things to fill in (`config.yaml`)

- **MATCHi prices.** MATCHi does not show prices on its schedule, so revenue for the two UK Padel
  clubs needs the non-member court price per hour in the `prices` table.
- **`realisation`.** Share of the listed price actually collected. Leave at 1.0 for list price,
  or lower it (e.g. 0.85) to allow for member discounts.
- **Hours.** Opening hours define the bookable court time. Check them against each club's app.

## When figures appear

A day is included once it has finished and was watched throughout. The first club figures show
the day after tracking starts; the 28-day view is meaningful after four weeks.

## How it works

- `tracker/collect.py` – every run, reads free slots for the next 14 days and records, for each
  30-minute court block, whether it is free, when it was first seen free and when it was taken.
- `tracker/analyse.py` – closes finished days into `data/blocks/<club>/<month>.csv` and builds
  `docs/data/summary.json` for the dashboard.
- `tracker/discover.py` – connection check.
- `docs/index.html` – the dashboard.

A block is **occupied** if it was not available at the last check before it started.
"Seen booked" blocks were watched going from free to taken; the rest were never offered publicly
(early bookings, members' advance window, coaching, leagues, maintenance). Blocks with no check
within 3 hours of their start are excluded. Revenue is court hire only.

## If something breaks

The dashboard shows the last error per club. Playtomic and MATCHi can change their internal
pages without notice; run `discover` and send the log to Claude to update the scraper.
GitHub pauses scheduled workflows in repositories with no activity for 60 days; if you see a
banner in the Actions tab, click "Enable workflow".

Use responsibly: checks are limited to every 30 minutes, public data only, for internal
analysis. The platforms' terms may restrict automated access.
