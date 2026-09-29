import json
import os
import platform
from pathlib import Path


APP_NAME = "RogueRenamer"

DEFAULT_CONFIG = {
    "tmdb": {
        "access_token": "",
        "api_key": "",
    },
    "tvdb": {
        "api_key": "",
        "pin": "",
    },
    "omdb": {
        "api_key": "",
    },
    "metadata": {
        "provider": "tmdb",
    },
    "naming": {
        "movie_template": "{title} ({year})",
        "tv_template": "{title} - S{season:02d}E{episode:02d} - {episode_title}",
    },
    "organization": {
        "enabled": False,
        "movie_library_root": "",
        "tv_library_root": "",
        "movie_folder_template": "{title} ({year})",
        "tv_folder_template": "{title} ({year})/Season {season:02d}",
    },
    "automation": {
        "auto_accept_threshold": 90,
        "review_all_matches": False,
    },
}


def get_config_dir():
    system = platform.system()

    if system == "Windows":
        base = os.getenv("APPDATA")

        if base:
            return Path(base) / APP_NAME

        return Path.home() / "AppData" / "Roaming" / APP_NAME

    xdg_config = os.getenv("XDG_CONFIG_HOME")

    if xdg_config:
        return Path(xdg_config) / APP_NAME

    return Path.home() / ".config" / APP_NAME


def get_config_file():
    return get_config_dir() / "config.json"


def get_history_file():
    return get_config_dir() / "rename_history.json"


def load_rename_history():
    history_file = get_history_file()

    if not history_file.exists():
        return []

    try:
        with open(history_file, "r", encoding="utf-8") as file:
            history = json.load(file)

        return history if isinstance(history, list) else []

    except (OSError, json.JSONDecodeError, TypeError):
        return []


def save_rename_history(history):
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)

    with open(get_history_file(), "w", encoding="utf-8") as file:
        json.dump(history, file, indent=4)


def load_config():
    config = {
        "tmdb": DEFAULT_CONFIG["tmdb"].copy(),
        "tvdb": DEFAULT_CONFIG["tvdb"].copy(),
        "omdb": DEFAULT_CONFIG["omdb"].copy(),
        "metadata": DEFAULT_CONFIG["metadata"].copy(),
        "naming": DEFAULT_CONFIG["naming"].copy(),
        "organization": DEFAULT_CONFIG["organization"].copy(),
        "automation": DEFAULT_CONFIG["automation"].copy(),
    }

    config_file = get_config_file()

    if not config_file.exists():
        return config

    try:
        with open(config_file, "r", encoding="utf-8") as file:
            saved = json.load(file)

        if isinstance(saved.get("tmdb"), dict):
            config["tmdb"].update(saved["tmdb"])

        if isinstance(saved.get("tvdb"), dict):
            config["tvdb"].update(saved["tvdb"])

        if isinstance(saved.get("omdb"), dict):
            config["omdb"].update(saved["omdb"])

        if isinstance(saved.get("metadata"), dict):
            config["metadata"].update(saved["metadata"])

        if isinstance(saved.get("naming"), dict):
            config["naming"].update(saved["naming"])

        if isinstance(saved.get("organization"), dict):
            config["organization"].update(saved["organization"])

        if isinstance(saved.get("automation"), dict):
            config["automation"].update(saved["automation"])

        return config

    except (OSError, json.JSONDecodeError, AttributeError):
        return config


def save_config(config):
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)

    config_file = get_config_file()

    with open(config_file, "w", encoding="utf-8") as file:
        json.dump(config, file, indent=4)
