import re
from difflib import SequenceMatcher

import requests

from app.settings import load_config

BASE_URL = "https://api4.thetvdb.com/v4"


class TVDBError(Exception):
    pass


_token_cache = None


def _config():
    return load_config().get("tvdb", {})


def _api_key():
    key = str(_config().get("api_key", "") or "").strip()
    if not key:
        raise TVDBError("TheTVDB API key has not been configured.")
    return key


def _pin():
    return str(_config().get("pin", "") or "").strip()


def login(force=False):
    global _token_cache
    if _token_cache and not force:
        return _token_cache

    payload = {"apikey": _api_key()}
    pin = _pin()
    if pin:
        payload["pin"] = pin

    try:
        response = requests.post(
            f"{BASE_URL}/login",
            json=payload,
            headers={"accept": "application/json", "Content-Type": "application/json"},
            timeout=15,
        )
    except requests.RequestException as error:
        raise TVDBError(f"Could not connect to TheTVDB: {error}") from error

    if response.status_code != 200:
        detail = ""
        try:
            body = response.json()
            detail = body.get("message") or body.get("status") or ""
        except Exception:
            pass
        suffix = f": {detail}" if detail else ""
        raise TVDBError(f"TheTVDB login returned HTTP {response.status_code}{suffix}")

    token = (response.json().get("data") or {}).get("token")
    if not token:
        raise TVDBError("TheTVDB login succeeded but no bearer token was returned.")

    _token_cache = token
    return token


def request_tvdb(endpoint, params=None, retry=True):
    try:
        response = requests.get(
            f"{BASE_URL}{endpoint}",
            headers={
                "Authorization": f"Bearer {login()}",
                "accept": "application/json",
            },
            params=params or {},
            timeout=15,
        )
    except requests.RequestException as error:
        raise TVDBError(f"Could not connect to TheTVDB: {error}") from error

    if response.status_code == 401 and retry:
        login(force=True)
        return request_tvdb(endpoint, params=params, retry=False)

    if response.status_code != 200:
        raise TVDBError(f"TheTVDB returned HTTP {response.status_code}")

    payload = response.json()
    return payload.get("data")


def test_connection():
    login(force=True)
    return True


def year_from_value(value):
    if value is None:
        return None
    match = re.search(r"\b(\d{4})\b", str(value))
    return int(match.group(1)) if match else None


def normalize_title(title):
    title = str(title or "").lower()
    title = re.sub(r"[^a-z0-9]+", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def title_similarity(first, second):
    first = normalize_title(first)
    second = normalize_title(second)
    if first == second:
        return 1.0
    return SequenceMatcher(None, first, second).ratio()


def confidence_label(score):
    if score >= 85:
        return "High"
    if score >= 50:
        return "Review"
    return "Low"


def add_reason(reasons, points, text):
    if points:
        reasons.append(f"{text} {'+' if points > 0 else ''}{points}")


def score_title(parsed_title, title, aliases=None):
    choices = [title] + list(aliases or [])
    similarity = max((title_similarity(parsed_title, value) for value in choices if value), default=0)
    reasons = []
    if similarity >= 0.995:
        points = 60
        add_reason(reasons, points, "Exact title")
    elif similarity >= 0.90:
        points = 52
        add_reason(reasons, points, "Very close title")
    elif similarity >= 0.80:
        points = 42
        add_reason(reasons, points, "Close title")
    elif similarity >= 0.65:
        points = 28
        add_reason(reasons, points, "Partial title")
    else:
        points = round(similarity * 30)
        add_reason(reasons, points, "Weak title")
    return points, reasons, similarity


def score_year(parsed_year, result_year):
    reasons = []
    if not parsed_year:
        return 0, reasons
    if not result_year:
        add_reason(reasons, -4, "Filename year but TheTVDB year unknown")
        return -4, reasons
    difference = abs(int(parsed_year) - int(result_year))
    if difference == 0:
        add_reason(reasons, 30, "Exact year")
        return 30, reasons
    if difference == 1:
        add_reason(reasons, 12, "Year off by one")
        return 12, reasons
    add_reason(reasons, -25, "Year mismatch")
    return -25, reasons


def finalize_score(points):
    return max(0, min(100, round(points)))


def _search(query, search_type):
    data = request_tvdb("/search", {"query": query, "type": search_type})
    return data if isinstance(data, list) else []


def _id(result):
    value = result.get("tvdb_id", result.get("id"))
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _aliases(result):
    values = result.get("aliases") or []
    return [str(v) for v in values if v]


def _image(result):
    value = result.get("image_url") or result.get("image")
    if value and str(value).startswith("http"):
        return str(value)
    return value


def _year(result):
    return (
        year_from_value(result.get("year"))
        or year_from_value(result.get("first_air_time"))
        or year_from_value(result.get("firstAired"))
    )


def _country(result):
    country = result.get("country")
    if isinstance(country, str) and country:
        return [country.upper()]
    return []


def search_movie_candidates(parsed):
    candidates = []
    for result in _search(parsed["title"], "movie")[:10]:
        title = result.get("name") or result.get("title") or parsed["title"]
        title_points, reasons, similarity = score_title(parsed["title"], title, _aliases(result))
        year = _year(result)
        year_points, year_reasons = score_year(parsed.get("year"), year)
        reasons.extend(year_reasons)
        score = finalize_score(title_points + year_points)
        candidates.append({
            "id": _id(result),
            "provider_id": _id(result),
            "provider": "tvdb",
            "provider_name": "TheTVDB",
            "type": "Movie",
            "title": title,
            "original_title": result.get("name") or "",
            "year": year,
            "overview": result.get("overview") or "",
            "poster_path": None,
            "poster_url": _image(result),
            "release_date": result.get("first_air_time") or "",
            "original_language": result.get("primary_language") or "",
            "origin_country": _country(result),
            "title_similarity": similarity,
            "score_reasons": reasons,
            "score": score,
        })
    candidates.sort(key=lambda item: item["score"], reverse=True)
    return candidates


def _series_episodes(series_id):
    # Official aired order. The v4 endpoint may paginate; follow page links when present.
    episodes = []
    page = 0
    while page < 100:
        payload = request_tvdb(
            f"/series/{series_id}/episodes/default",
            {"page": page},
        )
        if not isinstance(payload, dict):
            break
        batch = payload.get("episodes") or []
        episodes.extend(batch)
        links = payload.get("links") or {}
        next_value = links.get("next")
        if next_value in (None, "", False):
            break
        page += 1
    return episodes


def get_tv_season(series_id, season_number):
    episodes = []
    for episode in _series_episodes(series_id):
        season = episode.get("seasonNumber")
        if season is None:
            season = episode.get("season_number")
        if season is None:
            season = episode.get("airedSeason")
        try:
            season = int(season)
        except (TypeError, ValueError):
            continue
        if season != int(season_number):
            continue

        number = episode.get("number")
        if number is None:
            number = episode.get("episodeNumber")
        if number is None:
            number = episode.get("episode_number")

        episodes.append({
            "id": episode.get("id"),
            "episode_number": number,
            "name": episode.get("name") or (
                f"Episode {number}" if number is not None else "Episode"
            ),
            "air_date": episode.get("aired") or episode.get("airDate"),
            "_tvdb": episode,
        })

    return {
        "id": f"{series_id}:{int(season_number)}",
        "season_number": int(season_number),
        "episodes": episodes,
    }


def get_tv_episode(series_id, season_number, episode_number):
    season = get_tv_season(series_id, season_number)
    for episode in season.get("episodes") or []:
        try:
            number = int(episode.get("episode_number"))
        except (TypeError, ValueError):
            continue
        if number == int(episode_number):
            return episode
    raise TVDBError(
        f"S{int(season_number):02d}E{int(episode_number):02d} "
        "was not found on TheTVDB."
    )


def search_tv_candidates(parsed):
    requested = parsed.get("episodes") or [parsed["episode"]]
    candidates = []

    for result in _search(parsed["title"], "series")[:10]:
        title = result.get("name") or parsed["title"]
        title_points, reasons, similarity = score_title(parsed["title"], title, _aliases(result))
        year = _year(result)
        year_points, year_reasons = score_year(parsed.get("year"), year)
        reasons.extend(year_reasons)
        score = finalize_score(title_points + year_points)

        episode_details = []
        missing = []
        try:
            season_data = get_tv_season(_id(result), parsed["season"])
            by_number = {}
            for item in season_data.get("episodes") or []:
                try:
                    by_number[int(item.get("episode_number"))] = item
                except (TypeError, ValueError):
                    pass
            for number in requested:
                item = by_number.get(int(number))
                if item:
                    episode_details.append({
                        "id": item.get("id"),
                        "episode": int(number),
                        "name": item.get("name") or f"Episode {number}",
                        "air_date": item.get("air_date"),
                    })
                else:
                    missing.append(int(number))
        except TVDBError:
            missing = [int(number) for number in requested]

        if episode_details and not missing:
            # A confirmed SxxExx against an exact/strong series match is powerful
            # evidence. Keep this below a standalone auto-accept amount so title
            # quality and ambiguity still matter.
            add_reason(reasons, 35, "All requested episodes found")
            score = finalize_score(score + 35)
        elif missing:
            add_reason(reasons, -40, "Requested episode(s) missing")
            score = finalize_score(score - 40)

        titles = [item["name"] for item in episode_details]
        candidates.append({
            "id": _id(result),
            "provider_id": _id(result),
            "provider": "tvdb",
            "provider_name": "TheTVDB",
            "type": "TV",
            "title": title,
            "original_title": result.get("name") or "",
            "year": year,
            "overview": result.get("overview") or "",
            "poster_path": None,
            "poster_url": _image(result),
            "first_air_date": result.get("first_air_time") or "",
            "original_language": result.get("primary_language") or "",
            "origin_country": _country(result),
            "title_similarity": similarity,
            "score_reasons": reasons,
            "season": parsed["season"],
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
    best["confidence"] = confidence_label(best["score"])

    best_title = normalize_title(best.get("title"))
    competing = []
    for candidate in candidates[1:]:
        if normalize_title(candidate.get("title")) != best_title:
            continue
        if parsed["type"] == "TV" and not candidate.get("episode_title"):
            continue
        if parsed["type"] == "Movie" and parsed.get("year"):
            if candidate.get("year") != parsed.get("year"):
                continue
        competing.append(candidate)

    if competing:
        has_hint = bool(parsed.get("year") or parsed.get("country_hint"))
        gap = best["score"] - competing[0].get("score", 0)
        if not has_hint or gap < 15:
            best["confidence"] = "Review"
            best["ambiguity_reason"] = "Multiple matching titles found"
    elif len(candidates) > 1:
        gap = best["score"] - candidates[1].get("score", 0)
        second_similarity = candidates[1].get("title_similarity", 0)
        # Even a high raw score should not auto-accept when another candidate is
        # genuinely close. This protects titles such as The Thing / The Office.
        if gap <= 8 and second_similarity >= 0.80:
            best["confidence"] = "Review"
            best["ambiguity_reason"] = "Multiple similar matches found"

    return best
