# Letterboxd Theatre-Filtered Alerts

Polls Letterboxd's official API for popular-this-week movies, filters for recent high-rated films, then uses TMDB release-date data to keep only movies with a recent US theatrical or limited-theatrical release. It prints the current matches and can notify through Telegram via Apprise when the matching set changes.

## Setup

Dependencies are managed with [uv](https://docs.astral.sh/uv/) via `pyproject.toml` and `uv.lock` (Python 3.12).

```bash
uv sync
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
- `--dry-run-notify` prints the digest as plain text and renders the chart to `logs/trends.png`, without sending anything or changing state.
- `--config PATH` uses a different config file.

## Digest

Each Telegram notification has three parts:

1. A poster card for each film that just joined the list.
2. A trend chart (`logs/trends.png`): one row per film, with columns for Letterboxd rating, new watches per day, and rank on the popular-this-week list over the last 30 days. Each column shares a y-scale, so films can be compared with each other.
3. The full current list. Each film shows its rating and change over the past week, its popularity rank and rank change, its total watches with recent watches per day and a momentum label (rising, steady or cooling), how long it has been in theatres, and how long it has been on the list.

The trends come from the daily snapshots in `logs/results_*.json`, so keep that directory around. Ratings go back to the first run. Rank and watch counts (from Letterboxd's `/csi/film/<slug>/stats/` fragment) are recorded from 2026-10-30 onward. Watches per day needs two days of data, and momentum needs about a week.

## Theatre Filter

This does not scrape showtimes or local ticket inventory. A movie is treated as still in theatres when TMDB has a configured-region release date of type `2` limited theatrical or `3` theatrical inside the configured recent window. Defaults are `region: US`, `theatrical_window_days: 90`, and `future_grace_days: 7`.

## Tests

```bash
uv run python -m unittest discover -s tests -v
```
