# How "in theatres" is decided

This does not scrape showtimes or local ticket inventory, and TMDB records when theatrical runs start, never when they end, so "in theatres" is an estimate.

A movie is treated as still in theatres when TMDB has a configured-region release date of type `2` limited theatrical or `3` theatrical inside the configured recent window. Defaults are `region: US`, `theatrical_window_days: 90`, and `future_grace_days: 7`. You can check a film's data on its TMDB release page: `https://www.themoviedb.org/movie/<tmdb id>/releases`.

How long a film has been out is counted from its first US opening, not from the release that put it in the window. When that release starts more than 45 days after the first opening, the film is labelled as a re-release (with TMDB's note, such as "4DX"), or as going wide if a limited release was followed by a wide one. Films with a US digital release on or before today get a STREAMING tag.

Before the theatre check, films are also filtered by Letterboxd rating (`filters.min_rating`, 4.0 in the example config) and release year (`filters.release_years_back`).
