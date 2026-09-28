import json
import os
import platform
from pathlib import Path


APP_NAME = "RogueRenamer"


def get_config_dir():
    system = platform.system()

    if system == "Windows":
        base = os.getenv("APPDATA")

        if base:
            return Path(base) / APP_NAME

        return Path.home() / "AppData" / "Roaming" / APP_NAME

    # Linux / other Unix-like systems
    xdg_config = os.getenv("XDG_CONFIG_HOME")

    if xdg_config:
        return Path(xdg_config) / APP_NAME

    return Path.home() / ".config" / APP_NAME


def get_config_file():
    return get_config_dir() / "config.json"


def load_config():
    config_file = get_config_file()

    if not config_file.exists():
        return {}

    try:
        with open(config_file, "r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(config):
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)

    config_file = get_config_file()

    with open(config_file, "w", encoding="utf-8") as file:
        json.dump(config, file, indent=4)