# Letterboxd Theatre-Filtered Alerts

Polls Letterboxd's official API for popular-this-week movies, filters for recent high-rated films, then uses TMDB release-date data to keep only movies with a recent US theatrical or limited-theatrical release. It prints the current matches and can notify through Telegram via Apprise when the matching set changes.

## Setup

Dependencies are managed with [uv](https://docs.astral.sh/uv/) via `pyproject.toml` and `uv.lock` (Python 3.12).

```bash
uv sync
uv run playwright install chromium   # headless browser that renders the digest image
```

```bash
cp config.yaml.example config.yaml
```

The default source is `letterboxd_csi`, which scrapes Letterboxd's public CSI film browser list with `curl_cffi` browser impersonation. Fill in TMDB credentials for the theatre filter.
Letterboxd sometimes blocks scraped requests (403/429). The client warms up a session on `/films/` first, then retries blocked or failed requests with exponential backoff (5s, 10s, 20s, ...), starting a fresh session after each block. Set `letterboxd_csi.max_attempts` in `config.yaml` to change the attempt count (default 5).
The script also loads a local `.env` file from the repo root before reading env-referenced config values.

## Run

```bash
uv run python scraper.py --once
```

For continuous monitoring (one check per day):

```bash
./run.sh
```

The first successful run saves `state.json` as a baseline and does not send a notification. After that, a digest is sent whenever movies join or leave the matching set, and once a week regardless (`notifications.digest_weekday`, default `friday`; `null` turns the weekly digest off).
Set `notifications.notify_on_first_run: true` in local `config.yaml` if you want the first run to send a digest too.

A run that suddenly matches nothing when the previous run matched films is treated as a bad scrape: state is left alone, and the empty result only counts if the next run is empty too.

Other flags:

- `--notify-latest` sends the digest now, whether or not anything changed. It does not update `state.json` or the notification log, so scheduled alerts are unaffected.
- `--dry-run-notify` renders the digest image to `logs/digest.png` (or prints the text fallback), without sending anything or changing state.
- `--config PATH` uses a different config file.

## Digest

Each notification is a single image (`logs/digest.png`), sent as a Telegram photo. It is built as an HTML page by `digest_image.py` and screenshotted with headless Chromium through Playwright. The image is a leaderboard with one row per matching film:

- **Poster, title, genres**, and how long the film has been in theatres (or when it opens).
- **Rating**, with its change over the past 7 days.
- **Watches / day**: a 21-day sparkline of new Letterboxd watches per day, the recent 3-day average, and a momentum label (rising, steady or cooling, from comparing the last 3 days with the 3 before).
- **Popularity**: the film's position among all films on Letterboxd's popular-this-week list, its 7-day change, and a 21-day sparkline.

Films that just joined the list are highlighted with a NEW tag. Films that left appear greyed out under "Dropped off". If the image cannot be rendered (for example, Chromium is missing), the run sends a text digest instead of failing. Other Apprise targets get the text digest with the image attached.

## Stored data

Every run writes `logs/results_<date>_<time>.json`. These snapshots are the history behind the trends, so keep the directory; it is git-ignored and exists only on the machine running the job.

- `movies`: each matching film with its rating, popularity rank and lifetime Letterboxd watch, like and list counts (from the `/csi/film/<slug>/stats/` fragment) plus TMDB metadata. Watches per day is derived from consecutive watch counts.
- `candidates`: every film fetched from the popular-this-week list (top `candidate_limit`) with its rank and rating, including the ones the filters dropped.

Ratings go back to the first run. Ranks, watch counts and candidates are recorded from 2026-10-30 onward. Watches per day needs two days of data, and momentum needs about a week.

## Theatre Filter

This does not scrape showtimes or local ticket inventory. A movie is treated as still in theatres when TMDB has a configured-region release date of type `2` limited theatrical or `3` theatrical inside the configured recent window. Defaults are `region: US`, `theatrical_window_days: 90`, and `future_grace_days: 7`.

## Tests

```bash
uv run python -m unittest discover -s tests -v
```
