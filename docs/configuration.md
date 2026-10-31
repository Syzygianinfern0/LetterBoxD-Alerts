# Configuration and running

## Setup

Dependencies are managed with [uv](https://docs.astral.sh/uv/) via `pyproject.toml` and `uv.lock` (Python 3.12).

```bash
uv sync
uv run playwright install chromium   # headless browser that renders the digest image
cp config.yaml.example config.yaml
```

Fill in TMDB credentials for the theatre filter, and one or more [Apprise](https://github.com/caronc/apprise) URLs under `notifications.urls` (for Telegram: `tgram://<bot_token>/<chat_id>`).

Config values written as `env:NAME` are read from the environment. The script loads a local `.env` file from the repo root first, so secrets can live there instead of in `config.yaml`.

## Letterboxd source

The default source is `letterboxd_csi`, which scrapes Letterboxd's public CSI film browser list with `curl_cffi` browser impersonation.

Letterboxd sometimes blocks scraped requests (403/429). The client warms up a session on `/films/` first, then retries blocked or failed requests with exponential backoff (5s, 10s, 20s, ...), starting a fresh session after each block. Set `letterboxd_csi.max_attempts` in `config.yaml` to change the attempt count (default 5).

## Running

```bash
uv run python scraper.py --once   # one check
./run.sh                          # one check per day, forever
```

The first successful run saves `state.json` as a baseline and does not send a notification. After that, a digest is sent whenever movies join or leave the matching set, and once a week regardless (`notifications.digest_weekday`, default `friday`; `null` turns the weekly digest off). Set `notifications.notify_on_first_run: true` if you want the first run to send a digest too.

A run that suddenly matches nothing when the previous run matched films is treated as a bad scrape: state is left alone, and the empty result only counts if the next run is empty too.

## Command-line flags

- `--once` runs a single check and exits (also the default with no flags).
- `--notify-latest` sends the digest now, whether or not anything changed. It does not update `state.json` or the notification log, so scheduled alerts are unaffected.
- `--dry-run-notify` renders the digest image to `logs/digest.png` (or prints the text fallback), without sending anything or changing state.
- `--config PATH` uses a different config file.

## Tests

```bash
uv run python -m unittest discover -s tests -v
```
