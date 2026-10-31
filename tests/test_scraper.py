import unittest
from datetime import date

from history import days_on_list, film_trend
from scraper import (
    FilmCandidate,
    LetterboxdCSIClient,
    MatchedMovie,
    already_notified,
    build_digest_message,
    diff_results,
    filter_candidates,
    is_unconfirmed_empty,
    parse_film_stats,
    sort_matches,
    theatre_status_from_release_dates,
    weekly_digest_due,
)


class ScraperTests(unittest.TestCase):
    def test_filter_candidates_excludes_below_min_rating(self):
        candidates = [
            FilmCandidate(key="1", title="Low", year=2026, rating=3.4, tmdb_id=1),
            FilmCandidate(key="2", title="Good", year=2026, rating=3.5, tmdb_id=2),
        ]

        filtered = filter_candidates(candidates, min_rating=3.5, min_year=2025)

        self.assertEqual([movie.title for movie in filtered], ["Good"])

    def test_sort_matches_prioritizes_interesting_rating(self):
        matches = [
            MatchedMovie("1", "Solid", 2026, 3.8, None, None, None, None, "ok"),
            MatchedMovie("2", "Great", 2026, 4.1, None, None, None, None, "ok"),
            MatchedMovie("3", "Also Great", 2026, 4.0, None, None, None, None, "ok"),
        ]

        sorted_matches = sort_matches(matches, interesting_rating=4.0)

        self.assertEqual([movie.title for movie in sorted_matches], ["Great", "Also Great", "Solid"])

    def test_theatre_status_includes_us_limited_and_theatrical_releases(self):
        payload = {
            "results": [
                {
                    "iso_3166_1": "US",
                    "release_dates": [
                        {"release_date": "2026-05-15T00:00:00.000Z", "type": 2},
                        {"release_date": "2026-05-20T00:00:00.000Z", "type": 3},
                    ],
                }
            ]
        }

        status = theatre_status_from_release_dates(
            payload,
            region="US",
            window_days=90,
            future_grace_days=7,
            today=date(2026, 6, 2),
            tmdb_id=123,
        )

        self.assertTrue(status.in_theatres)
        self.assertEqual(status.theatrical_release_date, "2026-05-20")
        self.assertIn("theatrical", status.reason)

    def test_theatre_status_excludes_non_us_releases_for_us_region(self):
        payload = {
            "results": [
                {
                    "iso_3166_1": "FR",
                    "release_dates": [{"release_date": "2026-05-20T00:00:00.000Z", "type": 3}],
                }
            ]
        }

        status = theatre_status_from_release_dates(
            payload,
            region="US",
            window_days=90,
            future_grace_days=7,
            today=date(2026, 6, 2),
            tmdb_id=123,
        )

        self.assertFalse(status.in_theatres)

    def test_diff_results_first_run_reports_all_added(self):
        current = [MatchedMovie("1", "New", 2026, 4.2, None, None, None, None, "ok")]

        added, removed = diff_results(None, current)

        self.assertEqual([movie.key for movie in added], ["1"])
        self.assertEqual(removed, [])

    def test_diff_results_detects_added_and_removed(self):
        previous = {"movies": [{"key": "old", "title": "Old"}, {"key": "keep", "title": "Keep"}]}
        current = [
            MatchedMovie("keep", "Keep", 2026, 3.8, None, None, None, None, "ok"),
            MatchedMovie("new", "New", 2026, 4.1, None, None, None, None, "ok"),
        ]

        added, removed = diff_results(previous, current)

        self.assertEqual([movie.key for movie in added], ["new"])
        self.assertEqual([movie["key"] for movie in removed], ["old"])

    def test_diff_results_unchanged_set_sends_nothing(self):
        previous = {"movies": [{"key": "same", "title": "Same"}]}
        current = [MatchedMovie("same", "Same", 2026, 3.8, None, None, None, None, "ok")]

        added, removed = diff_results(previous, current)

        self.assertEqual(added, [])
        self.assertEqual(removed, [])

    def test_letterboxd_csi_extracts_candidates(self):
        html = """
        <ul>
          <li class="poster-container" data-average-rating="4.31">
            <div class="react-component"
                 data-postered-identifier='{"lid":"pEeQ","uid":"film:611288","type":"film"}'
                 data-item-slug="project-hail-mary"
                 data-item-name="Project Hail Mary (2026)"
                 data-target-link="/film/project-hail-mary/"></div>
          </li>
        </ul>
        """

        candidates = LetterboxdCSIClient({}).extract_candidates(html)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].title, "Project Hail Mary")
        self.assertEqual(candidates[0].year, 2026)
        self.assertEqual(candidates[0].rating, 4.31)
        self.assertEqual(candidates[0].letterboxd_id, "pEeQ")
        self.assertEqual(candidates[0].letterboxd_url, "https://letterboxd.com/film/project-hail-mary/")

    def test_letterboxd_csi_fetches_mixed_this_week_and_limits_candidates(self):
        class FakeCSIClient(LetterboxdCSIClient):
            def __init__(self):
                super().__init__({})
                self.requests = []

            def get_text(self, url, referer=None):
                self.requests.append((url, referer))
                items = []
                for index in range(30):
                    items.append(
                        f"""
                        <li class="poster-container" data-average-rating="3.80">
                          <div class="react-component"
                               data-postered-identifier='{{"lid":"film{index}","uid":"film:{index}","type":"film"}}'
                               data-item-slug="movie-{index}"
                               data-item-name="Movie {index} (2026)"></div>
                        </li>
                        """
                    )
                return "<ul>" + "".join(items) + "</ul>"

        client = FakeCSIClient()

        candidates = client.fetch_popular_this_week({})

        self.assertEqual(
            client.requests,
            [("https://letterboxd.com/csi/films/films-browser-list/popular/this/week/", None)],
        )
        self.assertEqual(len(candidates), 25)
        self.assertEqual(candidates[0].title, "Movie 0")
        self.assertEqual(candidates[-1].title, "Movie 24")

    def test_letterboxd_csi_uses_films_page_as_default_referer(self):
        class FakeResponse:
            status_code = 200
            text = "ok"

            def raise_for_status(self):
                pass

        class FakeSession:
            def __init__(self):
                self.headers = None

            def get(self, _url, **kwargs):
                self.headers = kwargs["headers"]
                return FakeResponse()

        client = LetterboxdCSIClient({})
        client.session = FakeSession()

        client.get_text("https://letterboxd.com/csi/example/")

        self.assertEqual(client.session.headers["referer"], "https://letterboxd.com/films/")


    def test_letterboxd_csi_records_popularity_rank(self):
        client = LetterboxdCSIClient({})
        client.get_text = lambda _url, referer=None: ""
        client.extract_candidates = lambda _html: [
            FilmCandidate(key="a", title="A", year=2026, rating=4.0),
            FilmCandidate(key="a", title="A", year=2026, rating=4.0),
            FilmCandidate(key="b", title="B", year=2026, rating=4.0),
        ]

        candidates = client.fetch_popular_this_week({})

        self.assertEqual([(c.key, c.popularity_rank) for c in candidates], [("a", 1), ("b", 2)])

    def test_parse_film_stats(self):
        html = (
            '<div class="production-statistic -watches" aria-label="Watched by 237,786&nbsp;members"></div>'
            '<div class="production-statistic -lists" aria-label="Appears in 68,615&nbsp;lists"></div>'
            '<div class="production-statistic -likes" aria-label="Liked by 127,894&nbsp;members"></div>'
        )

        self.assertEqual(parse_film_stats(html), {"watches": 237786, "lists": 68615, "likes": 127894})

    def test_already_notified_only_matches_most_recent_change(self):
        # A film that leaves, returns and leaves again must be reported each time.
        log = ["remove:1", "add:1"]

        self.assertFalse(already_notified(log, "remove:1"))
        self.assertTrue(already_notified(log, "add:1"))
        self.assertFalse(already_notified([], "add:1"))

    def test_unconfirmed_empty_requires_a_second_empty_run(self):
        previous = {"movies": [{"key": "1"}]}

        self.assertTrue(is_unconfirmed_empty([], previous))
        self.assertFalse(is_unconfirmed_empty([], {**previous, "unconfirmed_empty": True}))
        self.assertFalse(is_unconfirmed_empty([], {"movies": []}))
        self.assertFalse(is_unconfirmed_empty([MatchedMovie("1", "A", 2026, 4.1, None, None, None, None, "ok")], previous))

    def test_weekly_digest_due_once_on_configured_weekday(self):
        friday = date(2026, 10, 30)
        config = {"notifications": {"digest_weekday": "Friday"}}

        self.assertTrue(weekly_digest_due(config, {}, friday))
        self.assertFalse(weekly_digest_due(config, {"last_notified_date": "2026-10-30"}, friday))
        self.assertFalse(weekly_digest_due(config, {}, date(2026, 10, 29)))
        self.assertFalse(weekly_digest_due({"notifications": {"digest_weekday": None}}, {}, friday))

    def test_days_on_list_ignores_missing_and_empty_days(self):
        history = {
            date(2026, 9, 1): {},
            date(2026, 9, 2): {"1": {}},
            date(2026, 9, 3): {},  # suspect empty scrape
            date(2026, 9, 6): {"1": {}},  # 9/4-9/5 not scraped
        }

        self.assertEqual(days_on_list(history, "1", date(2026, 9, 6)), 5)
        self.assertEqual(days_on_list({date(2026, 9, 6): {"2": {}}}, "1", date(2026, 9, 6)), 0)

    def test_film_trend_rating_change_and_momentum(self):
        watches = [900, 1000, 1100, 1200, 1300, 1600, 1900, 2200]
        history = {
            date(2026, 10, day): {"1": {"rating": 4.0 + 0.01 * i, "watches": watches[i], "popularity_rank": 10 - i}}
            for i, day in enumerate(range(23, 31))
        }

        trend = film_trend(history, "1", date(2026, 10, 30))

        self.assertEqual(trend.watches_per_day, 300)
        self.assertEqual(trend.momentum, "rising")
        self.assertEqual(trend.rank_change_7d, 7)
        self.assertEqual(trend.rating_change_7d, 0.07)
        self.assertEqual(trend.days_on_list, 8)
        self.assertIsNone(film_trend(history, "1", date(2026, 10, 29)).rating_change_7d)  # no point a week before

    def test_digest_lists_every_current_film_and_changes(self):
        today = date(2026, 10, 30)
        matches = [
            MatchedMovie("1", "Kept", 2026, 4.4, None, "2026-10-16", "https://letterboxd.com/film/kept/", None, "ok", watches=5000),
            MatchedMovie("2", "New & Bold", 2026, 4.1, None, "2026-11-06", None, None, "ok"),
        ]

        message = build_digest_message(matches, [matches[1]], [{"key": "3", "title": "Gone"}], {}, today, "Picks")

        self.assertIn("1. <b>Kept</b>", message)
        self.assertIn("2. <b>New &amp; Bold</b> (2026) 🆕", message)
        self.assertIn("👋 Gone left", message)
        self.assertIn("In theatres since Oct 16 (2 wks)", message)
        self.assertIn("Opens Nov 6", message)
        self.assertIn("👁 5K watched", message)


if __name__ == "__main__":
    unittest.main()
