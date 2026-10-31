import argparse
import html
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests
import yaml
from apprise import Apprise
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests
from tabulate import tabulate


CONFIG_FILE = "config.yaml"
ENV_FILE = ".env"
STATE_FILE = "state.json"
LOGS_DIR = "logs"
NOTIFICATION_LOG_FILE = os.path.join(LOGS_DIR, "notifications.json")
LETTERBOXD_BASE_URL = "https://api.letterboxd.com/api/v0"
LETTERBOXD_WEB_URL = "https://letterboxd.com"
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE_URL = "https://image.tmdb.org/t/p/w780"

# Letterboxd intermittently answers scraped requests with 403/429. A single
# blocked request used to abort the whole daily run, so retry before giving up.
RETRY_STATUS_CODES = {403, 408, 429, 500, 502, 503, 504}
MAX_FETCH_ATTEMPTS = 5
RETRY_BASE_DELAY = 5.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)


@dataclass(frozen=True)
class FilmCandidate:
    key: str
    title: str
    year: int | None
    rating: float
    letterboxd_id: str | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    release_date: str | None = None
    letterboxd_url: str | None = None


@dataclass(frozen=True)
class TheatreStatus:
    in_theatres: bool
    reason: str
    tmdb_id: int | None = None
    tmdb_url: str | None = None
    theatrical_release_date: str | None = None


@dataclass(frozen=True)
class MatchedMovie:
    key: str
    title: str
    year: int | None
    rating: float
    release_date: str | None
    theatrical_release_date: str | None
    letterboxd_url: str | None
    tmdb_url: str | None
    theatre_reason: str
    poster_url: str | None = None
    genres: list[str] | None = None
    runtime_minutes: int | None = None
    overview: str | None = None
    trailer_url: str | None = None


def load_config(path: str = CONFIG_FILE) -> dict[str, Any]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"No {path} found. Copy config.yaml.example to config.yaml and fill in credentials.")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_env_file(path: str = ENV_FILE) -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def resolve_secret(value: Any) -> str | None:
    """Allow config values to be literal strings or env references."""
    if value is None:
        return None
    if isinstance(value, dict) and value.get("env"):
        return os.environ.get(str(value["env"]))
    if isinstance(value, str) and value.startswith("env:"):
        return os.environ.get(value.split(":", 1)[1])
    return str(value)


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def parse_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def clean_letterboxd_title(raw_name: str) -> tuple[str, int | None]:
    match = re.search(r"\s+\((\d{4})\)\s*$", raw_name)
    if not match:
        return raw_name.strip(), None
    return raw_name[: match.start()].strip(), int(match.group(1))


def get_path(data: Any, paths: list[list[str]]) -> Any:
    for path in paths:
        value = data
        for key in path:
            if not isinstance(value, dict) or key not in value:
                break
            value = value[key]
        else:
            if value not in (None, ""):
                return value
    return None


def find_external_id(data: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        value = data.get(name)
        if value not in (None, ""):
            return value

    for link_key in ("links", "identifiers", "externalIds", "external_ids"):
        links = data.get(link_key)
        if isinstance(links, dict):
            for name in names:
                value = links.get(name)
                if value not in (None, ""):
                    return value
        elif isinstance(links, list):
            for item in links:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or item.get("name") or item.get("site") or "").lower()
                if any(name.lower().replace("_", "") in item_type.replace("_", "") for name in names):
                    value = item.get("id") or item.get("identifier") or item.get("value")
                    if value not in (None, ""):
                        return value
    return None


def normalize_letterboxd_item(item: dict[str, Any]) -> FilmCandidate | None:
    film = item.get("film") if isinstance(item.get("film"), dict) else item
    rating = parse_float(
        get_path(
            film,
            [
                ["averageRating"],
                ["rating"],
                ["ratings", "average"],
                ["statistics", "rating"],
                ["statistics", "averageRating"],
            ],
        )
    )
    if rating is None:
        rating = parse_float(get_path(item, [["averageRating"], ["rating"]]))
    if rating is None:
        return None

    title = get_path(film, [["name"], ["title"], ["displayName"]])
    if not title:
        return None

    letterboxd_id = get_path(film, [["id"], ["lid"], ["filmId"]])
    tmdb_id = parse_int(find_external_id(film, ("tmdbId", "tmdb_id", "tmdb")))
    imdb_id = find_external_id(film, ("imdbId", "imdb_id", "imdb"))
    year = parse_int(get_path(film, [["releaseYear"], ["year"]]))
    release_date = get_path(film, [["releaseDate"], ["released"], ["release", "date"]])
    parsed_release = parse_date(release_date)
    if parsed_release and year is None:
        year = parsed_release.year

    slug = get_path(film, [["slug"], ["urlSlug"]])
    url = get_path(film, [["url"], ["links", "letterboxd"]])
    if not url and slug:
        url = f"https://letterboxd.com/film/{slug}/"

    key = str(tmdb_id or letterboxd_id or imdb_id or f"{title}-{year or ''}")
    return FilmCandidate(
        key=key,
        title=str(title),
        year=year,
        rating=rating,
        letterboxd_id=str(letterboxd_id) if letterboxd_id else None,
        tmdb_id=tmdb_id,
        imdb_id=str(imdb_id) if imdb_id else None,
        release_date=str(release_date)[:10] if release_date else None,
        letterboxd_url=str(url) if url else None,
    )


def extract_letterboxd_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("items", "films", "results", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def filter_candidates(
    candidates: list[FilmCandidate],
    min_rating: float,
    min_year: int | None,
) -> list[FilmCandidate]:
    filtered = []
    for candidate in candidates:
        if candidate.rating < min_rating:
            continue
        if min_year is not None and candidate.year is not None and candidate.year < min_year:
            continue
        if not (candidate.tmdb_id or candidate.imdb_id or candidate.title):
            continue
        filtered.append(candidate)
    return filtered


def sort_matches(matches: list[MatchedMovie], interesting_rating: float) -> list[MatchedMovie]:
    return sorted(
        matches,
        key=lambda movie: (
            movie.rating < interesting_rating,
            -(movie.rating or 0),
            movie.title.lower(),
        ),
    )


def diff_results(previous: dict[str, Any] | None, current: list[MatchedMovie]) -> tuple[list[MatchedMovie], list[dict[str, Any]]]:
    previous_movies = {}
    if previous:
        previous_movies = {item["key"]: item for item in previous.get("movies", []) if isinstance(item, dict) and item.get("key")}
    current_movies = {movie.key: movie for movie in current}

    added = [current_movies[key] for key in sorted(current_movies.keys() - previous_movies.keys())]
    removed = [previous_movies[key] for key in sorted(previous_movies.keys() - current_movies.keys())]
    return added, removed


class LetterboxdClient:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.base_url = str(config.get("base_url") or LETTERBOXD_BASE_URL).rstrip("/")
        self.session = requests.Session()
        self._access_token = resolve_secret(config.get("access_token"))

    def access_token(self) -> str:
        if self._access_token:
            return self._access_token

        client_id = resolve_secret(self.config.get("client_id"))
        client_secret = resolve_secret(self.config.get("client_secret"))
        if not client_id or not client_secret:
            raise RuntimeError("Letterboxd access_token or client_id/client_secret must be configured.")

        response = self.session.post(
            f"{self.base_url}/auth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            timeout=20,
        )
        response.raise_for_status()
        token_payload = response.json()
        self._access_token = token_payload["access_token"]
        return self._access_token

    def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}{path}",
            params=params,
            headers={"Authorization": f"Bearer {self.access_token()}", "Accept": "application/json"},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def fetch_popular_this_week(self, filters: dict[str, Any]) -> list[FilmCandidate]:
        per_page = int(filters.get("per_page", 100))
        max_pages = int(filters.get("max_pages", 2))
        release_years_back = int(filters.get("release_years_back", 1))
        current_year = datetime.now().year
        years = list(range(current_year, current_year - release_years_back - 1, -1))

        candidates: list[FilmCandidate] = []
        seen_keys: set[str] = set()

        for year in years:
            cursor = None
            for _ in range(max_pages):
                params: dict[str, Any] = {
                    "sort": "FilmPopularityThisWeek",
                    "where": "Released",
                    "year": year,
                    "perPage": per_page,
                    "excludeMemberFilmRelationships": "true",
                }
                if cursor:
                    params["cursor"] = cursor

                payload = self.get("/films", params)
                for raw_item in extract_letterboxd_items(payload):
                    candidate = normalize_letterboxd_item(raw_item)
                    if candidate and candidate.key not in seen_keys:
                        candidates.append(candidate)
                        seen_keys.add(candidate.key)

                cursor = get_path(payload, [["next"], ["cursor", "next"], ["pagination", "next"]])
                if not cursor:
                    break

        return candidates


class LetterboxdCSIClient:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.base_url = str(config.get("web_url") or LETTERBOXD_WEB_URL).rstrip("/")
        self.impersonate = str(config.get("impersonate", "chrome"))
        self.session = curl_requests.Session()
        self.max_attempts = int(config.get("max_attempts", MAX_FETCH_ATTEMPTS))
        self._warmed_up = False

    def warm_up(self) -> None:
        """Pick up cookies from a normal page view before hitting /csi/ endpoints."""
        if self._warmed_up:
            return
        self._warmed_up = True
        try:
            self.session.get(
                f"{self.base_url}/films/",
                impersonate=self.impersonate,
                timeout=30,
                headers={"accept-language": "en-US,en;q=0.9"},
            )
        except Exception as exc:
            logging.warning("Letterboxd warm-up request failed, continuing anyway: %s", exc)

    def get_text(self, url: str, referer: str | None = None) -> str:
        self.warm_up()
        last_exc: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.get(
                    url,
                    impersonate=self.impersonate,
                    timeout=30,
                    headers={
                        "referer": referer or f"{self.base_url}/films/",
                        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "accept-language": "en-US,en;q=0.9",
                    },
                )
                status = response.status_code
                if status in RETRY_STATUS_CODES and attempt < self.max_attempts:
                    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1)
                    logging.warning(
                        "Letterboxd returned HTTP %s for %s (attempt %d/%d), retrying in %.1fs",
                        status, url, attempt, self.max_attempts, delay,
                    )
                    time.sleep(delay)
                    # A fresh session drops any cookie the WAF flagged.
                    self.session = curl_requests.Session()
                    self._warmed_up = False
                    self.warm_up()
                    continue
                response.raise_for_status()
                return response.text
            except Exception as exc:
                last_exc = exc
                if attempt >= self.max_attempts:
                    break
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1)
                logging.warning(
                    "Letterboxd request for %s failed (attempt %d/%d): %s; retrying in %.1fs",
                    url, attempt, self.max_attempts, exc, delay,
                )
                time.sleep(delay)

        raise RuntimeError(f"Letterboxd request failed after {self.max_attempts} attempts: {url}") from last_exc

    def fetch_popular_this_week(self, filters: dict[str, Any]) -> list[FilmCandidate]:
        candidate_limit = int(filters.get("candidate_limit", 25))
        candidates: list[FilmCandidate] = []
        seen_keys: set[str] = set()

        url = f"{self.base_url}/csi/films/films-browser-list/popular/this/week/"
        html = self.get_text(url)
        page_candidates = self.extract_candidates(html)
        logging.info("Letterboxd CSI mixed this-week page returned %d candidates", len(page_candidates))

        for candidate in page_candidates:
            if candidate.key in seen_keys:
                continue
            candidates.append(candidate)
            seen_keys.add(candidate.key)
            if len(candidates) >= candidate_limit:
                break

        return candidates

    def extract_candidates(self, html: str) -> list[FilmCandidate]:
        soup = BeautifulSoup(html, "lxml")
        items = soup.select("li.poster-container, li.posteritem, li.griditem")
        candidates: list[FilmCandidate] = []

        for item in items:
            rating = parse_float(item.get("data-average-rating"))
            if rating is None:
                continue
            data_div = item.select_one(".react-component, [data-component-class='LazyPoster'], [data-item-slug], [data-film-slug]")
            if not data_div:
                continue

            raw_name = data_div.get("data-item-name") or data_div.get("data-film-name")
            slug = data_div.get("data-item-slug") or data_div.get("data-film-slug")
            if not raw_name or not slug:
                continue

            title, year = clean_letterboxd_title(raw_name)
            letterboxd_id = None
            internal_id = data_div.get("data-film-id")
            posted_identifier = data_div.get("data-postered-identifier")
            if posted_identifier:
                try:
                    identifier = json.loads(posted_identifier)
                    letterboxd_id = identifier.get("lid")
                    uid = identifier.get("uid", "")
                    if not internal_id and ":" in uid:
                        internal_id = uid.split(":", 1)[1]
                except json.JSONDecodeError:
                    pass

            url = f"{self.base_url}/film/{slug}/"
            key = str(letterboxd_id or internal_id or slug)
            candidates.append(
                FilmCandidate(
                    key=key,
                    title=title,
                    year=year,
                    rating=rating,
                    letterboxd_id=str(letterboxd_id) if letterboxd_id else None,
                    release_date=str(year) if year else None,
                    letterboxd_url=url,
                )
            )

        return candidates

    def hydrate_tmdb_ids(self, candidates: list[FilmCandidate]) -> list[FilmCandidate]:
        hydrated: list[FilmCandidate] = []
        for candidate in candidates:
            if candidate.tmdb_id or not candidate.letterboxd_url:
                hydrated.append(candidate)
                continue
            try:
                html = self.get_text(candidate.letterboxd_url, referer=f"{self.base_url}/films/")
                soup = BeautifulSoup(html, "lxml")
                body = soup.select_one("body")
                tmdb_id = parse_int(body.get("data-tmdb-id") if body else None)
                if tmdb_id is None:
                    match = re.search(r'data-tmdb-id=["\'](\d+)["\']', html)
                    tmdb_id = parse_int(match.group(1)) if match else None
                hydrated.append(
                    FilmCandidate(
                        key=str(tmdb_id or candidate.key),
                        title=candidate.title,
                        year=candidate.year,
                        rating=candidate.rating,
                        letterboxd_id=candidate.letterboxd_id,
                        tmdb_id=tmdb_id,
                        imdb_id=candidate.imdb_id,
                        release_date=candidate.release_date,
                        letterboxd_url=candidate.letterboxd_url,
                    )
                )
            except Exception as exc:
                logging.warning("Could not hydrate TMDB ID for %s: %s", candidate.title, exc)
                hydrated.append(candidate)
        return hydrated


class TMDBClient:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.base_url = str(config.get("base_url") or TMDB_BASE_URL).rstrip("/")
        self.session = requests.Session()
        self.bearer_token = resolve_secret(config.get("bearer_token"))
        self.api_key = resolve_secret(config.get("api_key"))
        if not self.bearer_token and not self.api_key:
            raise RuntimeError("TMDB bearer_token or api_key must be configured.")

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(params or {})
        headers = {"Accept": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        else:
            params["api_key"] = self.api_key

        response = self.session.get(f"{self.base_url}{path}", params=params, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()

    def find_movie(self, candidate: FilmCandidate, region: str) -> int | None:
        if candidate.tmdb_id:
            return candidate.tmdb_id
        if candidate.imdb_id:
            payload = self.get(f"/find/{candidate.imdb_id}", {"external_source": "imdb_id"})
            results = payload.get("movie_results") or []
            if results:
                return parse_int(results[0].get("id"))

        params: dict[str, Any] = {"query": candidate.title, "include_adult": "false", "region": region}
        if candidate.year:
            params["year"] = candidate.year
        payload = self.get("/search/movie", params)
        results = payload.get("results") or []
        if results:
            return parse_int(results[0].get("id"))
        return None

    def movie_details(self, tmdb_id: int) -> dict[str, Any]:
        return self.get(f"/movie/{tmdb_id}", {"append_to_response": "videos", "language": "en-US"})

    def theatre_status(
        self,
        candidate: FilmCandidate,
        region: str,
        window_days: int,
        future_grace_days: int,
        today: date | None = None,
    ) -> TheatreStatus:
        today = today or date.today()
        tmdb_id = self.find_movie(candidate, region)
        if not tmdb_id:
            return TheatreStatus(False, "No TMDB match")

        payload = self.get(f"/movie/{tmdb_id}/release_dates")
        status = theatre_status_from_release_dates(
            payload,
            region=region,
            window_days=window_days,
            future_grace_days=future_grace_days,
            today=today,
            tmdb_id=tmdb_id,
        )
        return status


def theatre_status_from_release_dates(
    payload: dict[str, Any],
    region: str,
    window_days: int,
    future_grace_days: int,
    today: date,
    tmdb_id: int | None = None,
) -> TheatreStatus:
    start = today - timedelta(days=window_days)
    end = today + timedelta(days=future_grace_days)
    region = region.upper()
    best_date: date | None = None
    best_type: int | None = None

    for country in payload.get("results", []):
        if not isinstance(country, dict) or str(country.get("iso_3166_1", "")).upper() != region:
            continue
        for release in country.get("release_dates", []):
            release_type = parse_int(release.get("type"))
            release_date = parse_date(release.get("release_date"))
            if release_type not in (2, 3) or release_date is None:
                continue
            if start <= release_date <= end and (best_date is None or release_date > best_date):
                best_date = release_date
                best_type = release_type

    tmdb_url = f"https://www.themoviedb.org/movie/{tmdb_id}" if tmdb_id else None
    if best_date:
        label = "limited theatrical" if best_type == 2 else "theatrical"
        return TheatreStatus(
            True,
            f"{region} {label} release on {best_date.isoformat()}",
            tmdb_id=tmdb_id,
            tmdb_url=tmdb_url,
            theatrical_release_date=best_date.isoformat(),
        )

    return TheatreStatus(False, f"No {region} theatrical release in the configured window", tmdb_id=tmdb_id, tmdb_url=tmdb_url)


def first_trailer_url(details: dict[str, Any]) -> str | None:
    videos = get_path(details, [["videos", "results"]])
    if not isinstance(videos, list):
        return None

    youtube_videos = [
        video
        for video in videos
        if isinstance(video, dict) and str(video.get("site", "")).lower() == "youtube" and video.get("key")
    ]
    trailers = [video for video in youtube_videos if str(video.get("type", "")).lower() == "trailer"]
    selected = (trailers or youtube_videos)[0] if youtube_videos else None
    if not selected:
        return None
    return f"https://youtu.be/{selected['key']}"


def movie_metadata(details: dict[str, Any]) -> dict[str, Any]:
    poster_path = details.get("poster_path")
    return {
        "poster_url": f"{TMDB_IMAGE_BASE_URL}{poster_path}" if poster_path else None,
        "genres": [str(genre["name"]) for genre in details.get("genres", []) if isinstance(genre, dict) and genre.get("name")],
        "runtime_minutes": parse_int(details.get("runtime")),
        "overview": str(details.get("overview")) if details.get("overview") else None,
        "trailer_url": first_trailer_url(details),
    }


def build_matches(candidates: list[FilmCandidate], tmdb_client: TMDBClient, filters: dict[str, Any]) -> list[MatchedMovie]:
    region = str(filters.get("region", "US")).upper()
    window_days = int(filters.get("theatrical_window_days", 90))
    future_grace_days = int(filters.get("future_grace_days", 7))
    matches: list[MatchedMovie] = []

    for candidate in candidates:
        try:
            status = tmdb_client.theatre_status(candidate, region, window_days, future_grace_days)
        except requests.RequestException as exc:
            logging.warning("TMDB check failed for %s: %s", candidate.title, exc)
            continue
        if not status.in_theatres:
            continue

        metadata: dict[str, Any] = {}
        if status.tmdb_id:
            try:
                metadata = movie_metadata(tmdb_client.movie_details(status.tmdb_id))
            except requests.RequestException as exc:
                logging.warning("TMDB metadata fetch failed for %s: %s", candidate.title, exc)

        matches.append(
            MatchedMovie(
                key=str(status.tmdb_id or candidate.key),
                title=candidate.title,
                year=candidate.year,
                rating=candidate.rating,
                release_date=candidate.release_date,
                theatrical_release_date=status.theatrical_release_date,
                letterboxd_url=candidate.letterboxd_url,
                tmdb_url=status.tmdb_url,
                theatre_reason=status.reason,
                **metadata,
            )
        )
    return matches


def load_json_file(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json_file(path: str, data: Any) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def state_payload(matches: list[MatchedMovie], timezone: str) -> dict[str, Any]:
    now = datetime.now(ZoneInfo(timezone))
    return {
        "last_updated": now.isoformat(),
        "movies": [asdict(movie) for movie in matches],
    }


def save_snapshot(matches: list[MatchedMovie], timezone: str) -> str:
    now = datetime.now(ZoneInfo(timezone))
    filename = os.path.join(LOGS_DIR, f"results_{now.strftime('%Y%m%d_%H%M%S')}.json")
    save_json_file(filename, state_payload(matches, timezone))
    return filename


def load_notification_log() -> list[str]:
    return load_json_file(NOTIFICATION_LOG_FILE, [])


def save_notification_log(keys: list[str]) -> None:
    save_json_file(NOTIFICATION_LOG_FILE, keys)


def movie_line(movie: MatchedMovie | dict[str, Any]) -> str:
    title = movie.title if isinstance(movie, MatchedMovie) else movie.get("title")
    rating = movie.rating if isinstance(movie, MatchedMovie) else movie.get("rating")
    year = movie.year if isinstance(movie, MatchedMovie) else movie.get("year")
    lb_url = movie.letterboxd_url if isinstance(movie, MatchedMovie) else movie.get("letterboxd_url")
    tmdb_url = movie.tmdb_url if isinstance(movie, MatchedMovie) else movie.get("tmdb_url")
    rating_text = f"{float(rating):.2f}" if rating is not None else "n/a"
    bits = [f"{title} ({year or 'n/a'}) - {rating_text} stars"]
    if lb_url:
        bits.append(str(lb_url))
    elif tmdb_url:
        bits.append(str(tmdb_url))
    return " | ".join(bits)


def build_notification_message(added: list[MatchedMovie], removed: list[dict[str, Any]], timezone: str) -> str:
    now = datetime.now(ZoneInfo(timezone)).strftime("%b %d, %Y %H:%M %Z")
    lines = ["Letterboxd theatre-filtered movies changed", f"Checked: {now}", ""]
    if added:
        lines.append("Added:")
        lines.extend(f"- {movie_line(movie)}" for movie in added)
        lines.append("")
    if removed:
        lines.append("Removed:")
        lines.extend(f"- {movie_line(movie)}" for movie in removed)
    return "\n".join(lines).strip()


def build_latest_message(matches: list[MatchedMovie], timezone: str) -> str:
    """The current set of matching films, for the on-demand --notify-latest push."""
    now = datetime.now(ZoneInfo(timezone)).strftime("%b %d, %Y %H:%M %Z")
    lines = ["Letterboxd theatre-filtered movies (latest)", f"Checked: {now}", ""]
    if matches:
        lines.extend(f"- {movie_line(movie)}" for movie in matches)
    else:
        lines.append("No movies currently match the configured constraints.")
    return "\n".join(lines).strip()


def runtime_text(minutes: int | None) -> str | None:
    if not minutes:
        return None
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def display_date(value: str | None) -> str | None:
    parsed = parse_date(value)
    if not parsed:
        return value
    return parsed.strftime("%b %d, %Y")


def shorten_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def build_movie_card_caption(movie: MatchedMovie) -> str:
    title = html.escape(movie.title)
    year = html.escape(str(movie.year or "n/a"))
    lines = [
        f"🎬 <b>{title} ({year})</b>",
        f"⭐ <b>Letterboxd:</b> {movie.rating:.2f}",
        "",
    ]

    if movie.genres:
        lines.append(f"🎭 {html.escape(' · '.join(movie.genres))}")
    runtime = runtime_text(movie.runtime_minutes)
    if runtime:
        lines.append(f"⏱ {runtime}")
    release = display_date(movie.theatrical_release_date)
    if release:
        lines.append(f"📅 US theatrical: {html.escape(release)}")
    if len(lines) > 3:
        lines.append("")

    if movie.overview:
        lines.extend([html.escape(shorten_text(movie.overview, 260)), ""])
    if movie.trailer_url:
        lines.append(f"🎞 Trailer: {html.escape(movie.trailer_url)}")
    if movie.letterboxd_url:
        lines.append(f"📍 Letterboxd: {html.escape(movie.letterboxd_url)}")
    elif movie.tmdb_url:
        lines.append(f"📍 TMDB: {html.escape(movie.tmdb_url)}")

    return "\n".join(lines).strip()


def parse_tgram_url(url: str) -> tuple[str, str] | None:
    if not url.startswith("tgram://"):
        return None
    token_and_chat = url.removeprefix("tgram://")
    if "/" not in token_and_chat:
        return None
    token, chat_id = token_and_chat.split("/", 1)
    chat_id = chat_id.split("?", 1)[0].strip("/")
    if not token or not chat_id:
        return None
    return token, chat_id


def telegram_api_request(token: str, method: str, data: dict[str, Any]) -> bool:
    response = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, timeout=30)
    if not response.ok:
        logging.error("Telegram %s failed: HTTP %s %s", method, response.status_code, response.text)
        return False
    payload = response.json()
    if not payload.get("ok"):
        logging.error("Telegram %s failed: %s", method, payload)
        return False
    return True


def send_telegram_movie_card(token: str, chat_id: str, movie: MatchedMovie) -> bool:
    caption = build_movie_card_caption(movie)
    if movie.poster_url:
        return telegram_api_request(
            token,
            "sendPhoto",
            {
                "chat_id": chat_id,
                "photo": movie.poster_url,
                "caption": caption,
                "parse_mode": "HTML",
            },
        )
    return telegram_api_request(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
    )


def send_telegram_notifications(
    telegram_urls: list[str],
    body: str,
    added: list[MatchedMovie],
    removed: list[dict[str, Any]],
) -> bool:
    success = True
    for url in telegram_urls:
        parsed = parse_tgram_url(url)
        if not parsed:
            logging.error("Unsupported Telegram notification URL")
            success = False
            continue
        token, chat_id = parsed
        if added:
            for movie in added:
                success = send_telegram_movie_card(token, chat_id, movie) and success
            if removed:
                removed_body = "Removed:\n" + "\n".join(f"- {movie_line(movie)}" for movie in removed)
                success = telegram_api_request(token, "sendMessage", {"chat_id": chat_id, "text": removed_body}) and success
        else:
            success = telegram_api_request(token, "sendMessage", {"chat_id": chat_id, "text": body}) and success
    return success


def send_notification(
    config: dict[str, Any],
    body: str,
    added: list[MatchedMovie] | None = None,
    removed: list[dict[str, Any]] | None = None,
) -> bool:
    urls = config.get("notifications", {}).get("urls", [])
    if not urls:
        logging.info("No notification URLs configured, skipping notification")
        return True

    added = added or []
    removed = removed or []
    telegram_urls = [str(url) for url in urls if str(url).startswith("tgram://")]
    apprise_urls = [str(url) for url in urls if not str(url).startswith("tgram://")]
    results = []

    if telegram_urls:
        results.append(send_telegram_notifications(telegram_urls, body, added, removed))

    if apprise_urls:
        apobj = Apprise()
        for url in apprise_urls:
            apobj.add(url)
        results.append(bool(apobj.notify(body=body)))

    result = all(results)
    if result:
        logging.info("Notification sent")
    else:
        logging.error("Notification failed")
    return result


def print_results(matches: list[MatchedMovie]) -> None:
    if not matches:
        print("No theatre-filtered movies matched the configured constraints.")
        return

    rows = [
        {
            "Title": movie.title,
            "Year": movie.year or "",
            "Rating": f"{movie.rating:.2f}",
            "Release": movie.release_date or "",
            "Theatre": movie.theatrical_release_date or "",
            "Reason": movie.theatre_reason,
            "Letterboxd": movie.letterboxd_url or "",
            "TMDB": movie.tmdb_url or "",
        }
        for movie in matches
    ]
    print(tabulate(rows, headers="keys", tablefmt="grid"))


def run_once(config: dict[str, Any], dry_run_notify: bool = False, notify_latest: bool = False) -> int:
    source = str(config.get("source", "letterboxd_csi"))
    filters = config.get("filters", {})
    timezone = str(config.get("timezone", "America/Chicago"))
    min_rating = float(filters.get("min_rating", 3.5))
    interesting_rating = float(filters.get("interesting_rating", 4.0))
    release_years_back = int(filters.get("release_years_back", 1))
    min_year = datetime.now().year - release_years_back

    tmdb_client = TMDBClient(config.get("tmdb", {}))

    logging.info("Fetching %s candidates", source)
    if source == "letterboxd_api":
        letterboxd_client = LetterboxdClient(config.get("letterboxd", {}))
        candidates = letterboxd_client.fetch_popular_this_week(filters)
    elif source == "letterboxd_csi":
        csi_client = LetterboxdCSIClient(config.get("letterboxd_csi", {}))
        candidates = csi_client.fetch_popular_this_week(filters)
    else:
        raise RuntimeError(f"Unsupported source: {source}")
    logging.info("Fetched %d Letterboxd candidates", len(candidates))

    candidates = filter_candidates(candidates, min_rating=min_rating, min_year=min_year)
    logging.info("%d candidates remain after rating/release filters", len(candidates))
    if source == "letterboxd_csi":
        candidates = csi_client.hydrate_tmdb_ids(candidates)

    matches = build_matches(candidates, tmdb_client, filters)
    matches = sort_matches(matches, interesting_rating)
    logging.info("%d movies remain after TMDB theatre filter", len(matches))

    print_results(matches)
    snapshot = save_snapshot(matches, timezone)
    logging.info("Saved snapshot to %s", snapshot)

    previous = load_json_file(STATE_FILE, None)
    added, removed = diff_results(previous, matches)
    first_run = previous is None

    if notify_latest:
        # On-demand push of the current state. Deliberately leaves state.json and
        # the notification log untouched so the scheduled change-only alerting
        # carries on exactly as before.
        message = build_latest_message(matches, timezone)
        if send_notification(config, message, added=matches, removed=[]):
            return 0
        return 1

    if dry_run_notify:
        print("\n--- Dry-run notification ---")
        if added or removed:
            print(build_notification_message(added, removed, timezone))
        else:
            print("No changes detected.")
        return 0

    notify_on_first_run = bool(config.get("notifications", {}).get("notify_on_first_run", False))
    if first_run and not notify_on_first_run:
        save_json_file(STATE_FILE, state_payload(matches, timezone))
        logging.info("First run: baseline saved without sending a change notification")
        return 0

    if not added and not removed:
        save_json_file(STATE_FILE, state_payload(matches, timezone))
        logging.info("No changes detected")
        return 0

    change_key = "|".join(sorted([f"add:{movie.key}" for movie in added] + [f"remove:{movie['key']}" for movie in removed]))
    notification_log = load_notification_log()
    if change_key in notification_log:
        save_json_file(STATE_FILE, state_payload(matches, timezone))
        logging.info("Change was already notified")
        return 0

    message = build_notification_message(added, removed, timezone)
    if send_notification(config, message, added=added, removed=removed):
        notification_log.append(change_key)
        save_notification_log(notification_log)
        save_json_file(STATE_FILE, state_payload(matches, timezone))
        return 0

    logging.error("State was not updated because notification failed")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="List and monitor high-rated Letterboxd popular movies still in theatres.")
    parser.add_argument("--once", action="store_true", help="Run one check and exit. This is the default behavior.")
    parser.add_argument("--dry-run-notify", action="store_true", help="Print the notification body without sending or changing state.")
    parser.add_argument(
        "--notify-latest",
        action="store_true",
        help="Send the current results to Telegram regardless of whether anything changed, without updating state.",
    )
    parser.add_argument("--config", default=CONFIG_FILE, help="Path to config YAML.")
    args = parser.parse_args()

    try:
        load_env_file()
        config = load_config(args.config)
        return run_once(config, dry_run_notify=args.dry_run_notify, notify_latest=args.notify_latest)
    except Exception as exc:
        logging.exception("Run failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
