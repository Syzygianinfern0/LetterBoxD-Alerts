#!/bin/bash

while true; do
    conda run -n Letterboxd python scraper.py --once

    # Wait for 1 day (86400 seconds)
    sleep 86400
done
