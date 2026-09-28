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


def parse_filename(filepath):
    path = Path(filepath)
    stem = path.stem

    # S01E01
    tv_match = re.search(
        r"(?i)(.*?)[ ._-]+S(\d{1,2})E(\d{1,3})",
        stem,
    )

    if tv_match:
        return {
            "type": "TV",
            "title": clean_title(tv_match.group(1)),
            "season": int(tv_match.group(2)),
            "episode": int(tv_match.group(3)),
            "year": None,
        }

    # 1x01
    tv_match = re.search(
        r"(?i)(.*?)[ ._-]+(\d{1,2})x(\d{1,3})",
        stem,
    )

    if tv_match:
        return {
            "type": "TV",
            "title": clean_title(tv_match.group(1)),
            "season": int(tv_match.group(2)),
            "episode": int(tv_match.group(3)),
            "year": None,
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
            "year": int(year_match.group(2)),
        }

    return {
        "type": "Unknown",
        "title": clean_title(stem),
        "season": None,
        "episode": None,
        "year": None,
    }