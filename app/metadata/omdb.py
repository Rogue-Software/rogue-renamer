import re
from difflib import SequenceMatcher

import requests

from app.settings import load_config

BASE_URL = "https://www.omdbapi.com/"


class OMDbError(Exception):
    pass


def _api_key():
    key = str(load_config().get("omdb", {}).get("api_key", "") or "").strip()
    if not key:
        raise OMDbError("OMDb API key has not been configured.")
    return key


def _request(params):
    query = dict(params or {})
    query["apikey"] = _api_key()
    query["r"] = "json"

    try:
        response = requests.get(BASE_URL, params=query, timeout=15)
        response.raise_for_status()
    except requests.RequestException as error:
        raise OMDbError(f"Could not connect to OMDb: {error}") from error

    try:
        data = response.json()
    except ValueError as error:
        raise OMDbError("OMDb returned an invalid response.") from error

    if str(data.get("Response", "True")).lower() == "false":
        raise OMDbError(data.get("Error") or "OMDb request failed.")

    return data


def test_connection():
    # A tiny known-title lookup validates the supplied key.
    data = _request({"i": "tt0133093", "plot": "short"})
    return bool(data.get("imdbID"))


def _year(value):
    match = re.search(r"\b(\d{4})\b", str(value or ""))
    return int(match.group(1)) if match else None


def _norm(value):
    value = str(value or "").lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _similarity(a, b):
    a, b = _norm(a), _norm(b)
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _confidence(score):
    if score >= 90:
        return "High"
    if score >= 50:
        return "Review"
    return "Low"


def _score_title(parsed_title, result_title):
    similarity = _similarity(parsed_title, result_title)
    reasons = []
    if similarity >= 0.995:
        points = 60
        reasons.append("Exact title +60")
    elif similarity >= 0.90:
        points = 52
        reasons.append("Very close title +52")
    elif similarity >= 0.80:
        points = 42
        reasons.append("Close title +42")
    elif similarity >= 0.65:
        points = 28
        reasons.append("Partial title +28")
    else:
        points = round(similarity * 30)
        reasons.append(f"Weak title +{points}")
    return points, reasons, similarity


def _score_year(parsed_year, result_year):
    if not parsed_year:
        return 0, []
    if not result_year:
        return -4, ["Filename year but OMDb year unknown -4"]
    diff = abs(int(parsed_year) - int(result_year))
    if diff == 0:
        return 30, ["Exact year +30"]
    if diff == 1:
        return 12, ["Year off by one +12"]
    return -25, ["Year mismatch -25"]


def _details(imdb_id):
    return _request({"i": imdb_id, "plot": "full"})


def _search(title, media_type):
    try:
        data = _request({"s": title, "type": media_type, "page": 1})
    except OMDbError as error:
        if "not found" in str(error).lower():
            return []
        raise
    return data.get("Search") or []


def _countries(details):
    raw = details.get("Country") or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _language(details):
    raw = details.get("Language") or ""
    return raw.split(",")[0].strip().lower() if raw else ""


def _poster(details):
    value = details.get("Poster")
    return value if value and value != "N/A" else None


def search_movie_candidates(parsed):
    candidates = []
    for result in _search(parsed["title"], "movie")[:10]:
        title = result.get("Title") or parsed["title"]
        title_points, reasons, similarity = _score_title(parsed["title"], title)
        year = _year(result.get("Year"))
        year_points, year_reasons = _score_year(parsed.get("year"), year)
        reasons.extend(year_reasons)

        imdb_id = result.get("imdbID")
        try:
            details = _details(imdb_id)
        except OMDbError:
            details = result

        score = max(0, min(100, round(title_points + year_points)))
        candidates.append({
            "id": imdb_id,
            "provider_id": imdb_id,
            "provider": "omdb",
            "provider_name": "OMDb",
            "type": "Movie",
            "title": details.get("Title") or title,
            "original_title": details.get("Title") or title,
            "year": _year(details.get("Year")) or year,
            "overview": "" if details.get("Plot") == "N/A" else details.get("Plot", ""),
            "poster_path": None,
            "poster_url": _poster(details) or (
                result.get("Poster") if result.get("Poster") != "N/A" else None
            ),
            "release_date": "" if details.get("Released") == "N/A" else details.get("Released", ""),
            "original_language": _language(details),
            "origin_country": _countries(details),
            "title_similarity": similarity,
            "score_reasons": reasons,
            "score": score,
        })

    candidates.sort(key=lambda item: item["score"], reverse=True)
    return candidates


def get_tv_season(series_id, season_number):
    data = _request({
        "i": series_id,
        "Season": int(season_number),
    })
    episodes = []
    for item in data.get("Episodes") or []:
        try:
            number = int(item.get("Episode"))
        except (TypeError, ValueError):
            continue
        episodes.append({
            "id": item.get("imdbID"),
            "episode_number": number,
            "name": item.get("Title") or f"Episode {number}",
            "air_date": None if item.get("Released") == "N/A" else item.get("Released"),
        })
    return {
        "id": f"{series_id}:{int(season_number)}",
        "season_number": int(season_number),
        "episodes": episodes,
    }


def get_tv_episode(series_id, season_number, episode_number):
    data = _request({
        "i": series_id,
        "Season": int(season_number),
        "Episode": int(episode_number),
        "plot": "full",
    })
    return {
        "id": data.get("imdbID"),
        "episode_number": int(episode_number),
        "name": data.get("Title") or f"Episode {int(episode_number)}",
        "air_date": None if data.get("Released") == "N/A" else data.get("Released"),
        "_omdb": data,
    }


def search_tv_candidates(parsed):
    requested = parsed.get("episodes") or [parsed["episode"]]
    candidates = []

    for result in _search(parsed["title"], "series")[:10]:
        title = result.get("Title") or parsed["title"]
        title_points, reasons, similarity = _score_title(parsed["title"], title)
        year = _year(result.get("Year"))
        year_points, year_reasons = _score_year(parsed.get("year"), year)
        reasons.extend(year_reasons)

        imdb_id = result.get("imdbID")
        try:
            details = _details(imdb_id)
        except OMDbError:
            details = result

        episode_details = []
        missing = []
        try:
            season = get_tv_season(imdb_id, parsed["season"])
            by_number = {
                int(ep["episode_number"]): ep
                for ep in season.get("episodes") or []
                if ep.get("episode_number") is not None
            }
            for number in requested:
                ep = by_number.get(int(number))
                if ep:
                    episode_details.append({
                        "id": ep.get("id"),
                        "episode": int(number),
                        "name": ep.get("name") or f"Episode {number}",
                        "air_date": ep.get("air_date"),
                    })
                else:
                    missing.append(int(number))
        except OMDbError:
            missing = [int(number) for number in requested]

        score = title_points + year_points
        if episode_details and not missing:
            score += 35
            reasons.append("All requested episodes found +35")
        elif missing:
            score -= 40
            reasons.append("Requested episode(s) missing -40")
        score = max(0, min(100, round(score)))

        titles = [ep["name"] for ep in episode_details]
        candidates.append({
            "id": imdb_id,
            "provider_id": imdb_id,
            "provider": "omdb",
            "provider_name": "OMDb",
            "type": "TV",
            "title": details.get("Title") or title,
            "original_title": details.get("Title") or title,
            "year": _year(details.get("Year")) or year,
            "overview": "" if details.get("Plot") == "N/A" else details.get("Plot", ""),
            "poster_path": None,
            "poster_url": _poster(details) or (
                result.get("Poster") if result.get("Poster") != "N/A" else None
            ),
            "first_air_date": "" if details.get("Released") == "N/A" else details.get("Released", ""),
            "original_language": _language(details),
            "origin_country": _countries(details),
            "title_similarity": similarity,
            "score_reasons": reasons,
            "season": int(parsed["season"]),
            "episode": int(requested[0]),
            "episodes": [int(v) for v in requested],
            "episode_title": " + ".join(titles) if titles and not missing else None,
            "episode_titles": titles,
            "episode_details": episode_details,
            "missing_episodes": missing,
            "score": score,
        })

    candidates.sort(key=lambda item: item["score"], reverse=True)
    return candidates


def get_candidates(parsed):
    if parsed["type"] == "Movie":
        return search_movie_candidates(parsed)
    if parsed["type"] == "TV":
        return search_tv_candidates(parsed)
    return []


def match_media(parsed):
    candidates = get_candidates(parsed)
    if not candidates:
        return None

    best = candidates[0].copy()
    best["candidates"] = [item.copy() for item in candidates]
    best["confidence"] = _confidence(best["score"])

    if len(candidates) > 1:
        second = candidates[1]
        gap = best["score"] - second.get("score", 0)
        if gap <= 8 and second.get("title_similarity", 0) >= 0.80:
            best["confidence"] = "Review"
            best["ambiguity_reason"] = "Multiple similar matches found"

    return best
