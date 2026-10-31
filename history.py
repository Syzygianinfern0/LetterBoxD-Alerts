"""Per-film trends over time, built from the daily snapshots in logs/.

Every run saves logs/results_<date>_<time>.json with the matching films, so the
snapshots already are the history: ratings go back to the first run, and the
popularity rank and watch counts are present from the run that started
recording them.
"""

import glob
import json
import os
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean
from typing import Any

SNAPSHOT_GLOB = "results_*.json"
# Momentum compares the average daily watch gain over the most recent
# MOMENTUM_WINDOW intervals against the window before it.
MOMENTUM_WINDOW = 3
RISING_RATIO = 1.15
COOLING_RATIO = 0.85


@dataclass(frozen=True)
class FilmTrend:
    days_on_list: int
    rating_change_7d: float | None
    watches_per_day: float | None
    momentum: str | None  # "rising", "steady" or "cooling"
    rank_change_7d: int | None  # positive = climbed the popular list


def load_history(logs_dir: str) -> dict[date, dict[str, dict[str, Any]]]:
    """{day: {film key: snapshot entry}}, keeping the last snapshot of each day."""
    history: dict[date, dict[str, dict[str, Any]]] = {}
    for path in sorted(glob.glob(os.path.join(logs_dir, SNAPSHOT_GLOB))):
        stamp = os.path.basename(path)[len("results_") : len("results_") + 8]
        try:
            day = date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (ValueError, OSError):
            continue
        movies = payload.get("movies", []) if isinstance(payload, dict) else []
        history[day] = {str(m["key"]): m for m in movies if isinstance(m, dict) and m.get("key")}
    return history


def film_series(history: dict[date, dict[str, dict[str, Any]]], key: str) -> list[tuple[date, dict[str, Any]]]:
    return [(day, films[key]) for day, films in sorted(history.items()) if key in films]


def value_on_or_before(series: list[tuple[date, dict[str, Any]]], field: str, day: date) -> Any:
    for point_day, entry in reversed(series):
        if point_day <= day and entry.get(field) is not None:
            return entry[field]
    return None


def daily_watch_gains(series: list[tuple[date, dict[str, Any]]]) -> list[tuple[date, float]]:
    """Watches gained per day between consecutive observations."""
    points = [(day, entry["watches"]) for day, entry in series if entry.get("watches") is not None]
    gains = []
    for (prev_day, prev), (day, cur) in zip(points, points[1:]):
        gap = (day - prev_day).days
        if gap > 0 and cur >= prev:
            gains.append((day, (cur - prev) / gap))
    return gains


def days_on_list(history: dict[date, dict[str, dict[str, Any]]], key: str, today: date) -> int:
    """Length of the current streak, counted from the first day of the streak.

    Days with no snapshot or an empty snapshot (a failed or suspect scrape) are
    treated as missing data rather than as the film leaving the list.
    """
    first_day = None
    for day in sorted((d for d in history if d <= today), reverse=True):
        films = history[day]
        if not films:
            continue
        if key not in films:
            break
        first_day = day
    return (today - first_day).days + 1 if first_day else 0


def film_trend(history: dict[date, dict[str, dict[str, Any]]], key: str, today: date) -> FilmTrend:
    series = film_series(history, key)
    week_ago = today - timedelta(days=7)

    rating_now = value_on_or_before(series, "rating", today)
    rating_then = value_on_or_before(series, "rating", week_ago)
    rating_change = round(rating_now - rating_then, 2) if rating_now is not None and rating_then is not None else None

    rank_now = value_on_or_before(series, "popularity_rank", today)
    rank_then = value_on_or_before(series, "popularity_rank", week_ago)
    rank_change = rank_then - rank_now if rank_now is not None and rank_then is not None else None

    gains = [gain for _, gain in daily_watch_gains(series)]
    watches_per_day = mean(gains[-MOMENTUM_WINDOW:]) if gains else None
    momentum = None
    if len(gains) >= MOMENTUM_WINDOW + 1:
        recent = mean(gains[-MOMENTUM_WINDOW:])
        earlier = mean(gains[-2 * MOMENTUM_WINDOW : -MOMENTUM_WINDOW])
        ratio = recent / earlier if earlier else float("inf")
        momentum = "rising" if ratio >= RISING_RATIO else "cooling" if ratio <= COOLING_RATIO else "steady"

    return FilmTrend(
        days_on_list=days_on_list(history, key, today),
        rating_change_7d=rating_change,
        watches_per_day=watches_per_day,
        momentum=momentum,
        rank_change_7d=rank_change,
    )


def compact_number(value: float) -> str:
    for threshold, suffix in ((1_000_000, "M"), (1_000, "K")):
        if abs(value) >= threshold:
            return f"{value / threshold:.1f}".rstrip("0").rstrip(".") + suffix
    return f"{value:.0f}"
