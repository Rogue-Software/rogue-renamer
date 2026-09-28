import re
from difflib import SequenceMatcher

import requests

from app.settings import load_config


BASE_URL = "https://api.themoviedb.org/3"


class TMDBError(Exception):
    pass


def get_headers():
    config = load_config()

    token = (
        config.get("tmdb", {})
        .get("access_token", "")
        .strip()
    )

    if not token:
        raise TMDBError(
            "TMDB access token has not been configured."
        )

    return {
        "Authorization": f"Bearer {token}",
        "accept": "application/json",
    }


def request_tmdb(endpoint, params=None):
    try:
        response = requests.get(
            f"{BASE_URL}{endpoint}",
            headers=get_headers(),
            params=params or {},
            timeout=15,
        )

    except requests.RequestException as error:
        raise TMDBError(
            f"Could not connect to TMDB: {error}"
        ) from error

    if response.status_code != 200:
        raise TMDBError(
            f"TMDB returned HTTP {response.status_code}"
        )

    return response.json()


def year_from_date(date_string):
    if not date_string:
        return None

    match = re.match(r"(\d{4})", date_string)

    if not match:
        return None

    return int(match.group(1))


def normalize_title(title):
    title = title.lower()

    title = re.sub(
        r"[^a-z0-9]+",
        " ",
        title,
    )

    return re.sub(
        r"\s+",
        " ",
        title,
    ).strip()


def title_similarity(first, second):
    first = normalize_title(first)
    second = normalize_title(second)

    if first == second:
        return 1.0

    return SequenceMatcher(
        None,
        first,
        second,
    ).ratio()


def confidence_label(score):
    if score >= 85:
        return "High"

    if score >= 50:
        return "Review"

    return "Low"


def score_movie_candidate(
    parsed,
    result,
):
    parsed_title = parsed["title"]

    tmdb_title = result.get(
        "title",
        "",
    )

    original_title = result.get(
        "original_title",
        "",
    )

    similarity = max(
        title_similarity(
            parsed_title,
            tmdb_title,
        ),
        title_similarity(
            parsed_title,
            original_title,
        ),
    )

    # Title contributes up to 75 points.
    score = similarity * 75

    parsed_year = parsed.get("year")

    result_year = year_from_date(
        result.get("release_date")
    )

    # A known year is extremely useful
    # for distinguishing remakes.
    if parsed_year and result_year:
        if parsed_year == result_year:
            score += 25

        elif abs(parsed_year - result_year) == 1:
            score += 10

        else:
            score -= 20

    return max(
        0,
        min(100, round(score)),
    )


def score_tv_candidate(
    parsed,
    result,
):
    parsed_title = parsed["title"]

    tmdb_title = result.get(
        "name",
        "",
    )

    original_title = result.get(
        "original_name",
        "",
    )

    similarity = max(
        title_similarity(
            parsed_title,
            tmdb_title,
        ),
        title_similarity(
            parsed_title,
            original_title,
        ),
    )

    # We don't normally have a year
    # in SxxExx filenames, so title
    # similarity carries most weight.
    score = similarity * 90

    popularity = result.get(
        "popularity",
        0,
    )

    # Tiny ranking bonus only.
    # Popularity must never overpower
    # a better title match.
    if popularity > 0:
        score += 5

    return max(
        0,
        min(100, round(score)),
    )


def search_movie_candidates(parsed):
    params = {
        "query": parsed["title"],
        "include_adult": "false",
    }

    # Don't restrict the TMDB search by year.
    # We want to see competing remakes too,
    # then score the year ourselves.

    data = request_tmdb(
        "/search/movie",
        params,
    )

    candidates = []

    for result in data.get(
        "results",
        []
    )[:10]:

        score = score_movie_candidate(
            parsed,
            result,
        )

        candidates.append(
            {
                "id": result["id"],
                "type": "Movie",
                "title": result.get(
                    "title",
                    parsed["title"],
                ),
                "original_title":
                    result.get(
                        "original_title",
                        "",
                    ),
                "year": year_from_date(
                    result.get(
                        "release_date"
                    )
                ),
                "overview":
                    result.get(
                        "overview",
                        "",
                    ),
                "poster_path":
                    result.get(
                        "poster_path"
                    ),
                "release_date": result.get("release_date"),
                "original_language": result.get("original_language", ""),
                "origin_country": result.get("origin_country", []),
                "popularity": result.get("popularity", 0),
                "score": score,
            }
        )

    candidates.sort(
        key=lambda item: item["score"],
        reverse=True,
    )

    return candidates


def search_tv_candidates(parsed):
    data = request_tmdb(
        "/search/tv",
        {
            "query": parsed["title"],
            "include_adult": "false",
        },
    )

    candidates = []
    requested_episodes = (
        parsed.get("episodes")
        or [parsed["episode"]]
    )

    for result in data.get("results", [])[:10]:
        score = score_tv_candidate(parsed, result)
        episode_details = []
        missing_episodes = []

        for episode_number in requested_episodes:
            try:
                episode_data = request_tmdb(
                    f"/tv/{result['id']}"
                    f"/season/{parsed['season']}"
                    f"/episode/{episode_number}"
                )

                episode_details.append(
                    {
                        "id": episode_data.get("id"),
                        "episode": episode_number,
                        "name": episode_data.get(
                            "name",
                            f"Episode {episode_number}",
                        ),
                        "air_date": episode_data.get("air_date"),
                    }
                )

            except TMDBError:
                missing_episodes.append(episode_number)

        if episode_details and not missing_episodes:
            score = min(100, score + 5)
        elif missing_episodes:
            # A multi-episode candidate is only valid if every requested
            # episode exists for the same show and season.
            score = max(0, score - 40)

        episode_titles = [
            item["name"]
            for item in episode_details
        ]

        candidates.append(
            {
                "id": result["id"],
                "type": "TV",
                "title": result.get(
                    "name",
                    parsed["title"],
                ),
                "original_title": result.get(
                    "original_name",
                    "",
                ),
                "year": year_from_date(
                    result.get("first_air_date")
                ),
                "overview": result.get("overview", ""),
                "poster_path": result.get("poster_path"),
                "first_air_date": result.get("first_air_date"),
                "original_language": result.get("original_language", ""),
                "origin_country": result.get("origin_country", []),
                "popularity": result.get("popularity", 0),
                "season": parsed["season"],
                "episode": requested_episodes[0],
                "episodes": requested_episodes.copy(),
                "episode_title": (
                    " + ".join(episode_titles)
                    if episode_titles and not missing_episodes
                    else None
                ),
                "episode_titles": episode_titles,
                "episode_details": episode_details,
                "missing_episodes": missing_episodes,
                "score": score,
            }
        )

    candidates.sort(
        key=lambda item: item["score"],
        reverse=True,
    )

    return candidates


def get_candidates(parsed):
    if parsed["type"] == "Movie":
        return search_movie_candidates(
            parsed
        )

    if parsed["type"] == "TV":
        return search_tv_candidates(
            parsed
        )

    return []


def match_media(parsed):
    candidates = get_candidates(parsed)

    if not candidates:
        return None

    # Copy the best candidate before attaching the candidate list.
    # Do NOT attach candidates to candidates[0] directly: that creates a
    # self-referential dictionary, which can cause PySide6/QVariant to
    # recurse until Windows reports a stack overflow when setData() is used.
    best = candidates[0].copy()

    best["candidates"] = [
        candidate.copy()
        for candidate in candidates
    ]
    best["confidence"] = confidence_label(
        best["score"]
    )

    # Detect genuinely ambiguous matches.
    # Example: The.Office.S02E03.mkv can match multiple
    # series named "The Office" that both contain S02E03.
    best_normalized = normalize_title(
        best["title"]
    )

    competing_exact_matches = []

    for candidate in candidates[1:]:
        candidate_normalized = normalize_title(
            candidate["title"]
        )

        if candidate_normalized != best_normalized:
            continue

        # For TV, only treat another exact-title result as
        # a serious competitor if the requested episode exists.
        if parsed["type"] == "TV":
            if not candidate.get("episode_title"):
                continue

        # A known movie year normally resolves remakes.
        if parsed["type"] == "Movie":
            parsed_year = parsed.get("year")

            if (
                parsed_year
                and candidate.get("year") != parsed_year
            ):
                continue

        competing_exact_matches.append(candidate)

    if competing_exact_matches:
        best["confidence"] = "Review"
        best["ambiguity_reason"] = (
            "Multiple matching titles found"
        )

    # Also review candidates whose scores are very close.
    elif len(candidates) > 1:
        second = candidates[1]

        difference = (
            best["score"]
            - second["score"]
        )

        if (
            best["score"] < 95
            and difference <= 8
        ):
            best["confidence"] = "Review"
            best["ambiguity_reason"] = (
                "Multiple similar matches found"
            )

    return best
