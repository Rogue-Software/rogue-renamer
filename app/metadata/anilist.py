import html
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher

import requests

BASE_URL = "https://graphql.anilist.co"


class AniListError(Exception):
    pass


MEDIA_FIELDS = """
    id
    idMal
    title { romaji english native }
    format
    status
    description(asHtml: false)
    startDate { year month day }
    episodes
    duration
    countryOfOrigin
    coverImage { large extraLarge }
    synonyms
"""


def _request(query, variables=None):
    try:
        response = requests.post(
            BASE_URL,
            json={"query": query, "variables": variables or {}},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "RogueRenamer/1.0",
            },
            timeout=15,
        )
    except requests.RequestException as error:
        raise AniListError(f"Could not connect to AniList: {error}") from error

    try:
        payload = response.json()
    except ValueError as error:
        raise AniListError("AniList returned an invalid response.") from error

    if response.status_code == 429:
        retry = response.headers.get("Retry-After")
        suffix = f" Try again in {retry} seconds." if retry else ""
        raise AniListError("AniList rate limit reached." + suffix)

    if not response.ok or payload.get("errors"):
        errors = payload.get("errors") or []
        message = errors[0].get("message") if errors else response.reason
        raise AniListError(f"AniList request failed: {message or response.status_code}")

    return payload.get("data") or {}


def test_connection():
    query = """
    query {
      Media(id: 1, type: ANIME) { id title { romaji } }
    }
    """
    data = _request(query)
    return bool((data.get("Media") or {}).get("id"))


def _norm(value):
    value = str(value or "").lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _similarity(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _titles(media):
    title = media.get("title") or {}
    values = [
        title.get("english"),
        title.get("romaji"),
        title.get("native"),
        *(media.get("synonyms") or []),
    ]
    result = []
    seen = set()
    for value in values:
        value = str(value or "").strip()
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _display_title(media):
    title = media.get("title") or {}
    return (
        title.get("english")
        or title.get("romaji")
        or title.get("native")
        or "Unknown Anime"
    )


def _original_title(media):
    title = media.get("title") or {}
    return title.get("romaji") or title.get("native") or _display_title(media)


def _year(media):
    return (media.get("startDate") or {}).get("year")


def _date(media):
    value = media.get("startDate") or {}
    year = value.get("year")
    month = value.get("month")
    day = value.get("day")
    if not year:
        return ""
    return f"{year:04d}-{month or 1:02d}-{day or 1:02d}"


def _description(media):
    text = media.get("description") or ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _poster(media):
    cover = media.get("coverImage") or {}
    return cover.get("extraLarge") or cover.get("large")


def _score_title(parsed_title, media):
    options = _titles(media)
    similarity = max((_similarity(parsed_title, title) for title in options), default=0.0)
    reasons = []

    if similarity >= 0.995:
        points = 60
        reasons.append("Exact anime title +60")
    elif similarity >= 0.90:
        points = 52
        reasons.append("Very close anime title +52")
    elif similarity >= 0.80:
        points = 42
        reasons.append("Close anime title +42")
    elif similarity >= 0.65:
        points = 28
        reasons.append("Partial anime title +28")
    else:
        points = round(similarity * 30)
        reasons.append(f"Weak anime title +{points}")

    return points, reasons, similarity


def _score_year(parsed_year, media_year):
    if not parsed_year:
        return 0, []
    if not media_year:
        return -4, ["Filename year but AniList year unknown -4"]

    diff = abs(int(parsed_year) - int(media_year))
    if diff == 0:
        return 30, ["Exact year +30"]
    if diff == 1:
        return 12, ["Year off by one +12"]
    return -25, ["Year mismatch -25"]


def _confidence(score):
    if score >= 90:
        return "High"
    if score >= 50:
        return "Review"
    return "Low"


def _search(title, formats):
    query = f"""
    query ($search: String!) {{
      Page(page: 1, perPage: 10) {{
        media(
          search: $search,
          type: ANIME,
          format_in: [{", ".join(formats)}],
          isAdult: false
        ) {{
          {MEDIA_FIELDS}
        }}
      }}
    }}
    """
    data = _request(query, {"search": title})
    return ((data.get("Page") or {}).get("media") or [])


def _get_media(media_id):
    query = f"""
    query ($id: Int!) {{
      Media(id: $id, type: ANIME) {{
        {MEDIA_FIELDS}
      }}
    }}
    """
    return _request(query, {"id": int(media_id)}).get("Media") or {}


def _episode_air_dates(media_id):
    # AniList's airing schedule is useful for air dates, but it does not
    # provide episode titles. Completed older shows may not have a complete
    # schedule, so callers must treat this as optional enrichment.
    query = """
    query ($id: Int!) {
      Media(id: $id, type: ANIME) {
        airingSchedule(page: 1, perPage: 50) {
          nodes { episode airingAt }
        }
      }
    }
    """
    try:
        data = _request(query, {"id": int(media_id)})
    except AniListError:
        return {}

    nodes = (
        ((data.get("Media") or {}).get("airingSchedule") or {}).get("nodes")
        or []
    )
    result = {}
    for node in nodes:
        number = node.get("episode")
        stamp = node.get("airingAt")
        if not number or not stamp:
            continue
        try:
            result[int(number)] = datetime.fromtimestamp(
                int(stamp), tz=timezone.utc
            ).date().isoformat()
        except (TypeError, ValueError, OSError):
            pass
    return result


def _base_candidate(media, parsed_type, score, reasons, similarity):
    return {
        "id": media.get("id"),
        "provider_id": media.get("id"),
        "provider": "anilist",
        "provider_name": "AniList",
        "type": parsed_type,
        "title": _display_title(media),
        "original_title": _original_title(media),
        "year": _year(media),
        "overview": _description(media),
        "poster_path": None,
        "poster_url": _poster(media),
        "release_date": _date(media),
        "first_air_date": _date(media),
        "original_language": "ja" if media.get("countryOfOrigin") == "JP" else "",
        "origin_country": [media.get("countryOfOrigin")] if media.get("countryOfOrigin") else [],
        "title_similarity": similarity,
        "score_reasons": reasons,
        "score": max(0, min(100, round(score))),
        "anilist_format": media.get("format"),
        "anilist_episode_count": media.get("episodes"),
        "mal_id": media.get("idMal"),
    }


def search_movie_candidates(parsed):
    candidates = []
    for media in _search(parsed["title"], ["MOVIE"]):
        title_points, reasons, similarity = _score_title(parsed["title"], media)
        year_points, year_reasons = _score_year(parsed.get("year"), _year(media))
        reasons.extend(year_reasons)

        candidate = _base_candidate(
            media,
            "Movie",
            title_points + year_points,
            reasons,
            similarity,
        )
        candidates.append(candidate)

    candidates.sort(key=lambda item: item["score"], reverse=True)
    return candidates


def search_tv_candidates(parsed):
    requested = [int(v) for v in (parsed.get("episodes") or [parsed["episode"]])]
    candidates = []

    # TV_SHORT/ONA/OVA are included because many anime libraries use normal
    # SxxExx filenames for them even though AniList does not classify them as TV.
    for media in _search(parsed["title"], ["TV", "TV_SHORT", "ONA", "OVA"]):
        title_points, reasons, similarity = _score_title(parsed["title"], media)
        year_points, year_reasons = _score_year(parsed.get("year"), _year(media))
        reasons.extend(year_reasons)

        episode_count = media.get("episodes")
        missing = []
        episode_details = []

        air_dates = _episode_air_dates(media.get("id"))
        for number in requested:
            if episode_count and number > int(episode_count):
                missing.append(number)
                continue
            episode_details.append({
                "id": f"{media.get('id')}:{number}",
                "episode": number,
                # AniList does not expose per-episode names in its public media API.
                "name": f"Episode {number}",
                "air_date": air_dates.get(number),
            })

        score = title_points + year_points
        if episode_details and not missing:
            score += 35
            reasons.append("Requested anime episode(s) valid +35")
        elif missing:
            score -= 40
            reasons.append("Requested episode(s) exceed AniList episode count -40")

        candidate = _base_candidate(
            media,
            "TV",
            score,
            reasons,
            similarity,
        )
        titles = [item["name"] for item in episode_details]
        candidate.update({
            "season": int(parsed["season"]),
            "episode": requested[0],
            "episodes": requested,
            "episode_title": " + ".join(titles) if titles and not missing else None,
            "episode_titles": titles,
            "episode_details": episode_details,
            "missing_episodes": missing,
        })
        candidates.append(candidate)

    candidates.sort(key=lambda item: item["score"], reverse=True)
    return candidates


def get_tv_season(series_id, season_number):
    media = _get_media(series_id)
    count = int(media.get("episodes") or 0)
    air_dates = _episode_air_dates(series_id)

    return {
        "id": f"{series_id}:{int(season_number)}",
        "season_number": int(season_number),
        "episodes": [
            {
                "id": f"{series_id}:{number}",
                "episode_number": number,
                "name": f"Episode {number}",
                "air_date": air_dates.get(number),
            }
            for number in range(1, count + 1)
        ],
    }


def get_tv_episode(series_id, season_number, episode_number):
    media = _get_media(series_id)
    count = int(media.get("episodes") or 0)
    number = int(episode_number)

    if count and number > count:
        raise AniListError(
            f"AniList entry has {count} episode(s); episode {number} does not exist."
        )

    air_date = _episode_air_dates(series_id).get(number)
    return {
        "id": f"{series_id}:{number}",
        "episode_number": number,
        "name": f"Episode {number}",
        "air_date": air_date,
    }


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
            best["ambiguity_reason"] = "Multiple similar AniList matches found"

    return best
