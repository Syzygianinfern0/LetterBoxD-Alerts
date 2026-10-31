#!/usr/bin/env bash
set -uo pipefail

cd "$(dirname "$0")"

trap 'exit 0' INT TERM

while true; do
    uv run python scraper.py --once

    # Wait 1 day
    sleep 86400
done
