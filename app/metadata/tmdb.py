import re

import requests

from app.settings import load_config


BASE_URL = "https://api.themoviedb.org/3"


class TMDBError(Exception):
    pass


def get_headers():
    config = load_config()

    token = (
        config
        .get("tmdb", {})
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


def search_movie(title, year=None):
    params = {
        "query": title,
        "include_adult": "false",
    }

    if year:
        params["year"] = year

    data = request_tmdb(
        "/search/movie",
        params,
    )

    results = data.get("results", [])

    if not results:
        return None

    # TMDB already ranks its search results.
    result = results[0]

    return {
        "id": result["id"],
        "title": result.get("title", title),
        "year": year_from_date(
            result.get("release_date")
        ),
        "overview": result.get("overview", ""),
    }


def search_tv(title):
    data = request_tmdb(
        "/search/tv",
        {
            "query": title,
            "include_adult": "false",
        },
    )

    results = data.get("results", [])

    if not results:
        return None

    result = results[0]

    return {
        "id": result["id"],
        "title": result.get("name", title),
        "year": year_from_date(
            result.get("first_air_date")
        ),
        "overview": result.get("overview", ""),
    }


def get_tv_episode(
    show_id,
    season,
    episode,
):
    try:
        data = request_tmdb(
            f"/tv/{show_id}/season/{season}/episode/{episode}"
        )

    except TMDBError:
        return None

    return {
        "id": data.get("id"),
        "name": data.get(
            "name",
            f"Episode {episode}",
        ),
        "air_date": data.get("air_date"),
    }


def match_media(parsed):
    media_type = parsed["type"]

    if media_type == "Movie":
        movie = search_movie(
            parsed["title"],
            parsed["year"],
        )

        if not movie:
            return None

        return {
            "type": "Movie",
            "tmdb_id": movie["id"],
            "title": movie["title"],
            "year": movie["year"],
            "episode_title": None,
        }

    if media_type == "TV":
        show = search_tv(
            parsed["title"]
        )

        if not show:
            return None

        episode = get_tv_episode(
            show["id"],
            parsed["season"],
            parsed["episode"],
        )

        if not episode:
            return None

        return {
            "type": "TV",
            "tmdb_id": show["id"],
            "title": show["title"],
            "year": show["year"],
            "season": parsed["season"],
            "episode": parsed["episode"],
            "episode_title": episode["name"],
        }

    return None