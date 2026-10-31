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

The first successful run saves `state.json` as a baseline and does not send a change notification. Later runs notify when movies are added to or removed from the matching set.
Set `notifications.notify_on_first_run: true` in local `config.yaml` if you want the first run to send the current matching list as a Telegram notification.

Other flags:

- `--notify-latest` sends the current matching list to Telegram whether or not anything changed. It does not update `state.json` or the notification log, so scheduled change alerts are unaffected.
- `--dry-run-notify` prints the notification body without sending it or changing state.
- `--config PATH` uses a different config file.

## Theatre Filter

This does not scrape showtimes or local ticket inventory. A movie is treated as still in theatres when TMDB has a configured-region release date of type `2` limited theatrical or `3` theatrical inside the configured recent window. Defaults are `region: US`, `theatrical_window_days: 90`, and `future_grace_days: 7`.

## Tests

```bash
uv run python -m unittest discover -s tests -v
```
