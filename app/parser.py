import re
from pathlib import Path


MEDIA_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".mov",
    ".m4v",
    ".wmv",
    ".ts",
    ".m2ts",
    ".mpg",
    ".mpeg",
}


COMPANION_EXTENSIONS = {
    ".srt",
    ".ass",
    ".ssa",
    ".sub",
    ".vtt",
    ".idx",
    ".nfo",
}

COMPANION_TAGS = {
    "forced",
    "sdh",
    "cc",
    "hi",
    "default",
    "commentary",
}


def split_companion_filename(filepath):
    """Return the likely video stem and suffix tags for a companion file."""
    path = Path(filepath)
    stem = path.stem
    parts = stem.split(".")

    tags = []

    # Preserve common language codes and subtitle flags at the end.
    while len(parts) > 1:
        candidate = parts[-1].lower()

        is_language = bool(
            re.fullmatch(r"[a-z]{2,3}(?:-[a-z]{2})?", candidate)
        )

        if candidate in COMPANION_TAGS or is_language:
            tags.insert(0, parts.pop())
        else:
            break

    return ".".join(parts), tags


def companion_suffix(filepath):
    """Return suffix such as '.en.forced.srt'."""
    path = Path(filepath)
    _, tags = split_companion_filename(filepath)

    tag_part = "".join(f".{tag}" for tag in tags)
    return f"{tag_part}{path.suffix.lower()}"


JUNK_WORDS = {
    "1080p",
    "2160p",
    "720p",
    "480p",
    "bluray",
    "blu-ray",
    "brrip",
    "bdrip",
    "webrip",
    "web-rip",
    "webdl",
    "web-dl",
    "hdtv",
    "x264",
    "x265",
    "h264",
    "h265",
    "hevc",
    "av1",
    "aac",
    "dts",
    "atmos",
    "hdr",
    "hdr10",
    "dv",
    "remux",
}


def clean_title(text):
    text = text.replace(".", " ")
    text = text.replace("_", " ")

    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\([^\)]*\)", " ", text)

    words = text.split()
    cleaned_words = []

    for word in words:
        if word.lower() in JUNK_WORDS:
            break

        cleaned_words.append(word)

    text = " ".join(cleaned_words)

    return re.sub(r"\s+", " ", text).strip()



REGION_HINTS = {
    "us": "US",
    "usa": "US",
    "uk": "GB",
    "gb": "GB",
    "au": "AU",
    "aus": "AU",
    "ca": "CA",
    "can": "CA",
    "nz": "NZ",
}


def extract_tv_hints(raw_title):
    normalized = re.sub(r"[._-]+", " ", raw_title)
    tokens = normalized.split()

    year = None
    country_hint = None
    kept = []

    for token in tokens:
        lower = token.lower().strip("()[]")

        if re.fullmatch(r"(?:19|20)\d{2}", lower):
            year = int(lower)
            continue

        if lower in REGION_HINTS:
            country_hint = REGION_HINTS[lower]
            continue

        kept.append(token)

    return {
        "title": clean_title(" ".join(kept)),
        "year": year,
        "country_hint": country_hint,
    }


def parse_filename(filepath):
    path = Path(filepath)
    stem = path.stem

    # S01E01, S01E01E02, S01E01-E02, S01E01.S01E02
    first = re.search(
        r"(?i)(.*?)[ ._-]+S(\d{1,2})E(\d{1,3})",
        stem,
    )

    if first:
        hints = extract_tv_hints(first.group(1))
        title = hints["title"]
        season = int(first.group(2))
        episodes = [int(first.group(3))]

        tail = stem[first.end():]

        # Additional episodes may be E02 or S01E02. Stop naturally
        # when release-info text begins.
        for match in re.finditer(
            r"(?i)(?:[ ._-]*)(?:S(\d{1,2}))?E(\d{1,3})",
            tail,
        ):
            extra_season = (
                int(match.group(1))
                if match.group(1)
                else season
            )

            if extra_season != season:
                break

            episode = int(match.group(2))
            if episode not in episodes:
                episodes.append(episode)

        return {
            "type": "TV",
            "title": title,
            "season": season,
            "episode": episodes[0],
            "episodes": episodes,
            "year": hints["year"],
            "country_hint": hints["country_hint"],
        }

    # 1x01, 1x01-1x02, 1x01x02
    first = re.search(
        r"(?i)(.*?)[ ._-]+(\d{1,2})x(\d{1,3})",
        stem,
    )

    if first:
        hints = extract_tv_hints(first.group(1))
        title = hints["title"]
        season = int(first.group(2))
        episodes = [int(first.group(3))]
        tail = stem[first.end():]

        for match in re.finditer(
            r"(?i)(?:[ ._-]*)(?:(\d{1,2})x)?(\d{1,3})",
            tail,
        ):
            # Avoid treating ordinary release-info numbers as episodes.
            token = match.group(0)
            if "x" not in token.lower():
                continue

            extra_season = (
                int(match.group(1))
                if match.group(1)
                else season
            )
            if extra_season != season:
                break

            episode = int(match.group(2))
            if episode not in episodes:
                episodes.append(episode)

        return {
            "type": "TV",
            "title": title,
            "season": season,
            "episode": episodes[0],
            "episodes": episodes,
            "year": hints["year"],
            "country_hint": hints["country_hint"],
        }

    # Movie with year
    year_match = re.search(
        r"(.*?)[ ._(\[]((?:19|20)\d{2})(?:[ ._)\]]|$)",
        stem,
    )

    if year_match:
        return {
            "type": "Movie",
            "title": clean_title(year_match.group(1)),
            "season": None,
            "episode": None,
            "episodes": [],
            "year": int(year_match.group(2)),
            "country_hint": None,
        }

    return {
        "type": "Unknown",
        "title": clean_title(stem),
        "season": None,
        "episode": None,
        "episodes": [],
        "year": None,
        "country_hint": None,
    }

