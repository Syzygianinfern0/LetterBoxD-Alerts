import unittest
from datetime import date

from scraper import (
    FilmCandidate,
    LetterboxdCSIClient,
    MatchedMovie,
    diff_results,
    filter_candidates,
    sort_matches,
    theatre_status_from_release_dates,
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
                self.requested_urls = []

            def get_text(self, url, referer=None):
                self.requested_urls.append(url)
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
            client.requested_urls,
            ["https://letterboxd.com/csi/films/films-browser-list/popular/this/week/"],
        )
        self.assertEqual(len(candidates), 25)
        self.assertEqual(candidates[0].title, "Movie 0")
        self.assertEqual(candidates[-1].title, "Movie 24")


if __name__ == "__main__":
    unittest.main()
