"""Render the digest as a single image: an HTML leaderboard screenshotted by
headless Chromium (Playwright).

One row per film with its poster, rating, watches-per-day sparkline and
popularity rank; films that just joined are highlighted and films that left
are listed underneath.
"""

import os
from datetime import date, timedelta
from html import escape
from typing import Any

from history import compact_number, daily_watch_gains, film_series, film_trend

SPARK_DAYS = 21
PAGE_WIDTH = 600
MAX_IMAGE_FILMS = 12
MOMENTUM = {
    "rising": ("↗", "Rising", "var(--rise)"),
    "steady": ("→", "Steady", "var(--steady)"),
    "cooling": ("↘", "Cooling", "var(--cool)"),
}
STEADY_COLOR = "#8b939b"

CSS = """
:root{--bg:#101316;--surface2:#222830;--line:#2c333b;--text:#f2f1ee;--muted:#9aa1a8;--faint:#6b737b;
--rise:#2fc28c;--cool:#e8733f;--steady:#8b939b;--new:#4f9bf0;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:Inter,Roboto,'Noto Sans',sans-serif;width:600px;-webkit-font-smoothing:antialiased}
.wrap{padding:28px 24px 22px}
.head{display:flex;align-items:flex-end;justify-content:space-between;margin-bottom:18px}
.eyebrow{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin-bottom:4px}
h1{font-size:26px;font-weight:700;letter-spacing:-.01em}
.chips{display:flex;gap:6px}
.chip{font-size:12px;font-weight:600;padding:5px 10px;border-radius:999px;background:var(--surface2);color:var(--muted)}
.chip.new{color:var(--new);background:rgba(79,155,240,.14)}
.d{font-size:12px;font-weight:600;font-variant-numeric:tabular-nums}.d.up{color:var(--rise)}.d.down{color:var(--cool)}.d.flat{color:var(--faint)}
.pill{display:inline-block;font-size:10px;font-weight:800;letter-spacing:.08em;padding:3px 6px;border-radius:4px;vertical-align:2px;background:var(--new);color:#fff}
.pill.streaming{background:transparent;color:var(--muted);box-shadow:inset 0 0 0 1px var(--faint)}
.cols,.row{display:grid;grid-template-columns:56px 1fr 62px 120px 78px;gap:12px}
.cols{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--faint);padding:0 12px 8px;border-bottom:1px solid var(--line)}
.cols span:nth-child(3),.cols span:nth-child(5){text-align:right}
.row{align-items:center;padding:10px 12px;border-bottom:1px solid var(--line);position:relative}
.row.new{background:linear-gradient(90deg,rgba(79,155,240,.16),rgba(79,155,240,0) 70%)}
.row.new::before{content:"";position:absolute;left:0;top:8px;bottom:8px;width:3px;border-radius:2px;background:var(--new)}
.poster{width:56px;height:84px;border-radius:5px;object-fit:cover;box-shadow:0 2px 8px rgba(0,0,0,.4);background:var(--surface2)}
.title{font-size:16px;font-weight:650;line-height:1.2;margin-bottom:4px}
.meta{font-size:12px;color:var(--muted)}
.rating,.rank{text-align:right}
.rating b{display:block;font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}
.rank b{display:block;font-size:18px;font-weight:700}
.mini{display:flex;justify-content:flex-end;margin-top:5px}
.buzz .num{font-size:12px;color:var(--muted);margin-top:4px;font-variant-numeric:tabular-nums}
.buzz .num b{color:var(--text);font-weight:600}
.collecting{font-size:11px;color:var(--faint);height:30px;display:flex;align-items:center}
.empty{padding:28px 12px;color:var(--muted);font-size:14px;border-bottom:1px solid var(--line)}
.left-strip{margin-top:18px;padding-top:14px;border-top:1px solid var(--line)}
.left-strip h3{font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:600;margin-bottom:10px}
.gone-list{display:flex;flex-wrap:wrap;gap:14px 24px}
.gone{display:flex;gap:12px;align-items:center}
.gone img{width:40px;height:60px;border-radius:4px;object-fit:cover;filter:grayscale(1);opacity:.55}
.gone .t{font-size:14px;color:var(--muted);text-decoration:line-through;text-decoration-color:var(--faint)}
.gone .s{font-size:12px;color:var(--faint);margin-top:2px}
.foot{margin-top:16px;font-size:11px;color:var(--faint);line-height:1.5}
"""


def short_date(value: date) -> str:
    return f"{value:%b} {value.day}"


def weeks_text(days: int) -> str:
    weeks = days // 7
    return f"{weeks} wk" + ("s" if weeks != 1 else "")


def parse_iso(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def release_lines(movie: Any, today: date) -> list[str]:
    """How long the film has been out, counted from its first US opening,
    plus what put it back in the window if that was a later run."""
    current = parse_iso(movie.theatrical_release_date)
    first = parse_iso(getattr(movie, "first_theatrical_date", None)) or current
    if not current or not first:
        return []
    if first > today:
        return [f"Opens {short_date(first)}"]

    run = getattr(movie, "current_run", None)
    if not run:
        days = (today - first).days
        return ["Opened this week" if days < 7 else f"In theatres {weeks_text(days)}"]

    lines = [f"Out {weeks_text((today - first).days)} (opened {short_date(first)})"]
    note = getattr(movie, "current_run_note", None) or ""
    if len(note) > 24:  # long TMDB notes ("Director's cut (4K restoration)") don't fit the row
        note = ""
    if run == "expansion":
        lines.append(f"Went wide {short_date(current)}")
    else:
        lines.append(f"Back {short_date(current)}: {note + ' ' if note else ''}re-release")
    return lines


def small_poster(url: str | None) -> str | None:
    # The w780 posters stored in state are far larger than a 56px thumbnail.
    return url.replace("/w780/", "/w342/") if url else None


def sparkline(values: list[float], width: int, height: int, color: str, *, fill: bool = True,
              invert: bool = False, stroke: float = 2, dot: float = 3) -> str:
    if invert:
        values = [-v for v in values]
    low, high = min(values), max(values)
    span = high - low or 1
    points = [
        (i * width / (len(values) - 1), height - 3 - (v - low) / span * (height - 6))
        for i, v in enumerate(values)
    ]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    area = f'<polygon points="0,{height} {line} {width},{height}" fill="{color}" opacity="0.14"/>' if fill else ""
    x, y = points[-1]
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" style="overflow:visible">{area}'
        f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="{stroke}" '
        f'stroke-linejoin="round" stroke-linecap="round"/><circle cx="{x:.1f}" cy="{y:.1f}" r="{dot}" fill="{color}"/></svg>'
    )


def delta_html(value: float | None, digits: int = 2) -> str:
    if not value:
        return '<span class="d flat">–</span>'
    arrow, cls = ("▲", "up") if value > 0 else ("▼", "down")
    return f'<span class="d {cls}">{arrow}{abs(value):.{digits}f}</span>'


def film_row(movie: Any, history: dict[date, dict[str, dict[str, Any]]], is_new: bool, today: date) -> str:
    trend = film_trend(history, movie.key, today)
    start = today - timedelta(days=SPARK_DAYS - 1)
    gains = [gain for day, gain in daily_watch_gains(film_series(history, movie.key)) if day >= start]
    ranks = [
        entry["popularity_rank"]
        for day, entry in film_series(history, movie.key)
        if day >= start and entry.get("popularity_rank") is not None
    ]

    title = escape(movie.title) + (' <span class="pill">NEW</span>' if is_new else "")
    if getattr(movie, "streaming_since", None):
        title += ' <span class="pill streaming">STREAMING</span>'
    meta = [escape(" · ".join((movie.genres or [])[:2]))] + [escape(line) for line in release_lines(movie, today)]

    if len(gains) >= 2:
        arrow, label, color = MOMENTUM.get(trend.momentum, ("", "", "var(--steady)"))
        spark_color = {"rising": "#2fc28c", "cooling": "#e8733f"}.get(trend.momentum, STEADY_COLOR)
        momentum = f' <span style="color:{color}">{arrow} {label}</span>' if trend.momentum else ""
        buzz = (
            sparkline(gains, 120, 30, spark_color)
            + f'<div class="num"><b>+{compact_number(trend.watches_per_day)}</b>/day{momentum}</div>'
        )
    elif movie.watches is not None:
        buzz = f'<div class="collecting">collecting data</div><div class="num"><b>{compact_number(movie.watches)}</b> watched</div>'
    else:
        buzz = '<div class="collecting">no data</div>'

    rank = ""
    if movie.popularity_rank:
        mini = ""
        if len(ranks) >= 2:
            mini = f'<div class="mini">{sparkline(ranks, 70, 18, STEADY_COLOR, fill=False, invert=True, stroke=1.5, dot=2.2)}</div>'
        rank = f"<b>#{movie.popularity_rank}</b>{delta_html(trend.rank_change_7d, 0)}{mini}"

    poster = small_poster(movie.poster_url)
    poster_html = f'<img class="poster" src="{escape(poster)}">' if poster else '<div class="poster"></div>'
    return (
        f'<div class="row{" new" if is_new else ""}">{poster_html}'
        f'<div><div class="title">{title}</div><div class="meta">{"<br>".join(m for m in meta if m)}</div></div>'
        f'<div class="rating"><b>{movie.rating:.2f}</b>{delta_html(trend.rating_change_7d)}</div>'
        f'<div class="buzz">{buzz}</div><div class="rank">{rank}</div></div>'
    )


def removed_strip(removed: list[dict[str, Any]]) -> str:
    if not removed:
        return ""
    items = []
    for movie in removed:
        poster = small_poster(movie.get("poster_url"))
        last = f"last ⭐ {float(movie['rating']):.2f}" if movie.get("rating") is not None else ""
        if movie.get("popularity_rank"):
            last += f", #{movie['popularity_rank']}"
        items.append(
            (f'<img src="{escape(poster)}">' if poster else "")
            + f'<div><div class="t">{escape(str(movie.get("title")))}</div><div class="s">Left the list{" · " + last if last else ""}</div></div>'
        )
    gone = "".join(f'<div class="gone">{item}</div>' for item in items)
    return f'<div class="left-strip"><h3>Dropped off</h3><div class="gone-list">{gone}</div></div>'


def digest_html(
    matches: list[Any],
    added: list[Any],
    removed: list[dict[str, Any]],
    history: dict[date, dict[str, dict[str, Any]]],
    today: date,
) -> str:
    chips = []
    if added:
        chips.append(f'<span class="chip new">+{len(added)} new</span>')
    if removed:
        chips.append(f'<span class="chip">−{len(removed)} left</span>')
    head = (
        f'<div class="head"><div><div class="eyebrow">In theatres · {today:%b} {today.day}</div>'
        f'<h1>Highly rated this week</h1></div><div class="chips">{"".join(chips)}</div></div>'
    )
    cols = "<div class=cols><span></span><span>Film</span><span>Rating</span><span>Watches / day</span><span>Popularity</span></div>"
    added_keys = {movie.key for movie in added}
    rows = "".join(film_row(movie, history, movie.key in added_keys, today) for movie in matches[:MAX_IMAGE_FILMS])
    if not matches:
        rows = '<div class="empty">No films currently match the configured constraints.</div>'
    elif len(matches) > MAX_IMAGE_FILMS:
        rows += f'<div class="empty">…and {len(matches) - MAX_IMAGE_FILMS} more</div>'
    foot = (
        '<div class="foot">▲/▼ = change over 7 days · watches/day = 3-day average<br>'
        "Popularity = position among all films on Letterboxd this week</div>"
    )
    body = head + cols + rows + removed_strip(removed) + foot
    return f"<!doctype html><html><head><meta charset=utf-8><style>{CSS}</style></head><body><div class=wrap>{body}</div></body></html>"


def render_digest_image(
    matches: list[Any],
    added: list[Any],
    removed: list[dict[str, Any]],
    history: dict[date, dict[str, dict[str, Any]]],
    today: date,
    path: str,
) -> str:
    from playwright.sync_api import sync_playwright

    page_html = digest_html(matches, added, removed, history, today)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": PAGE_WIDTH, "height": 800}, device_scale_factor=2)
            page.set_content(page_html, wait_until="networkidle", timeout=60_000)
            page.locator(".wrap").screenshot(path=path)
        finally:
            browser.close()
    return path
