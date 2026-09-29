import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
import re
import subprocess
import json
import shutil

from PySide6.QtCore import Qt, QByteArray, QObject, QThread, Signal, QUrl
from PySide6.QtGui import QPixmap, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QFrame,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QScrollBar,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QScrollArea,
    QStackedWidget,
    QButtonGroup,
    QRadioButton,
)

import requests

from app.metadata import tvdb, omdb, anilist

from app.metadata.providers import (
    MetadataProviderError as TMDBError,
    available_providers,
    get_active_provider_id,
    get_active_provider_name,
    get_tv_episode,
    get_tv_season,
    match_media,
    set_active_provider,
)
from app.parser import (
    COMPANION_EXTENSIONS,
    MEDIA_EXTENSIONS,
    companion_suffix,
    classify_companion,
    is_extra_video,
    parse_filename,
    parse_media_path,
    split_companion_filename,
)
from app.settings import (
    load_config,
    save_config,
    load_rename_history,
    save_rename_history,
)


def safe_filename(text):
    illegal = '<>:"/\\|?*'

    for character in illegal:
        text = text.replace(character, "")

    return text.strip()


DEFAULT_MOVIE_TEMPLATE = "{title} ({year})"
DEFAULT_TV_TEMPLATE = "{title} - S{season:02d}E{episode:02d} - {episode_title}"

DEFAULT_MOVIE_FOLDER_TEMPLATE = "{title} ({year})"
DEFAULT_TV_FOLDER_TEMPLATE = "{title} ({year})/Season {season:02d}"


def get_organization_settings():
    config = load_config()
    organization = config.get("organization", {})

    return {
        "enabled": bool(organization.get("enabled", False)),
        "movie_library_root": organization.get(
            "movie_library_root",
            "",
        ),
        "tv_library_root": organization.get(
            "tv_library_root",
            "",
        ),
        "movie_folder_template": organization.get(
            "movie_folder_template",
            DEFAULT_MOVIE_FOLDER_TEMPLATE,
        ),
        "tv_folder_template": organization.get(
            "tv_folder_template",
            DEFAULT_TV_FOLDER_TEMPLATE,
        ),
    }


def render_folder_template(template, values):
    try:
        rendered = template.format(**values)
    except (KeyError, ValueError, IndexError) as error:
        raise ValueError(
            f"Invalid folder template: {error}"
        ) from error

    parts = []

    for raw_part in rendered.replace("\\", "/").split("/"):
        cleaned = safe_filename(" ".join(raw_part.split()))
        cleaned = cleaned.strip(" .")

        if cleaned:
            parts.append(cleaned)

    if not parts:
        raise ValueError("Folder template produced an empty path.")

    return Path(*parts)


def build_destination_path(original_path, match, proposed_name):
    source = Path(original_path)
    organization = get_organization_settings()

    if not organization["enabled"]:
        return source.with_name(proposed_name)

    if match["type"] == "Movie":
        values = {
            "title": match.get("title", ""),
            "year": match.get("year") or "",
            "tmdb_id": match.get("id") or "",
            "provider_id": match.get("id") or "",
        }
        relative_folder = render_folder_template(
            organization["movie_folder_template"],
            values,
        )
    else:
        values = {
            "title": match.get("title", ""),
            "year": match.get("year") or "",
            "season": match.get("season") or 0,
            "episode": match.get("episode") or 0,
            "episodes": format_episode_numbers(match),
            "episode_code": format_episode_code(match),
            "episode_title": match.get("episode_title") or "",
            "tmdb_id": match.get("id") or "",
            "provider_id": match.get("id") or "",
        }
        relative_folder = render_folder_template(
            organization["tv_folder_template"],
            values,
        )

    root_value = (
        organization["movie_library_root"]
        if match["type"] == "Movie"
        else organization["tv_library_root"]
    )
    root_value = str(root_value or "").strip()

    # Backward-compatible fallback: if no library root is configured,
    # organize beneath the source folder as older Rogue versions did.
    root = Path(root_value).expanduser() if root_value else source.parent

    return root / relative_folder / proposed_name


def get_naming_templates():
    config = load_config()
    naming = config.get("naming", {})
    return (
        naming.get("movie_template", DEFAULT_MOVIE_TEMPLATE),
        naming.get("tv_template", DEFAULT_TV_TEMPLATE),
    )


def render_naming_template(template, values):
    try:
        filename = template.format(**values)
    except (KeyError, ValueError, IndexError) as error:
        raise ValueError(f"Invalid naming template: {error}") from error

    filename = " ".join(filename.split())
    filename = filename.strip(" -._")
    return safe_filename(filename)



def format_episode_code(match):
    season = match.get("season") or 0
    episodes = match.get("episodes") or [match.get("episode") or 0]

    if len(episodes) == 1:
        return f"S{season:02d}E{episodes[0]:02d}"

    return (
        f"S{season:02d}E{episodes[0]:02d}"
        + "".join(f"-E{episode:02d}" for episode in episodes[1:])
    )


def format_episode_numbers(match):
    episodes = match.get("episodes") or [match.get("episode") or 0]
    if len(episodes) == 1:
        return f"{episodes[0]:02d}"
    return "-".join(f"{episode:02d}" for episode in episodes)


def build_proposed_filename(
    original_path,
    match,
):
    path = Path(original_path)
    extension = path.suffix
    movie_template, tv_template = get_naming_templates()

    if match["type"] == "Movie":
        values = {
            "title": match.get("title", ""),
            "year": match.get("year") or "",
            "tmdb_id": match.get("id") or "",
            "provider_id": match.get("id") or "",
        }
        filename = render_naming_template(movie_template, values)
    else:
        episodes = match.get("episodes") or [match.get("episode") or 0]
        values = {
            "title": match.get("title", ""),
            "year": match.get("year") or "",
            "season": match.get("season") or 0,
            "episode": match.get("episode") or 0,
            "episodes": format_episode_numbers(match),
            "episode_code": format_episode_code(match),
            "episode_title": match.get("episode_title") or "",
            "tmdb_id": match.get("id") or "",
            "provider_id": match.get("id") or "",
        }

        # Preserve the user's existing preset for single episodes. For
        # multi-episode files, make the stock preset produce an unambiguous
        # S01E01-E02 style name automatically.
        if (
            len(episodes) > 1
            and tv_template == DEFAULT_TV_TEMPLATE
        ):
            filename = (
                f"{values['title']} - {values['episode_code']} - "
                f"{values['episode_title']}"
            )
            filename = render_naming_template(filename, values)
        else:
            filename = render_naming_template(tv_template, values)

    return filename + extension


def find_companion_files(video_path):
    """Find subtitles, NFOs and artwork that safely belong to one video."""
    video = Path(video_path)
    companions = []

    if not video.parent.exists():
        return companions

    video_stem = video.stem.casefold()

    for candidate in video.parent.iterdir():
        if not candidate.is_file():
            continue
        if candidate.suffix.lower() not in COMPANION_EXTENSIONS:
            continue

        base_stem, _tags = split_companion_filename(candidate)

        # Only attach sidecars whose base name identifies this exact video.
        # Generic poster.jpg/fanart.jpg remains untouched so it cannot be
        # accidentally assigned to one episode in a multi-episode folder.
        if base_stem.casefold() == video_stem:
            companions.append(candidate)

    return sorted(
        companions,
        key=lambda item: item.name.casefold(),
    )


def build_companion_destination(video_destination, companion_path):
    """Rename a sidecar to the renamed video's stem while preserving tags."""
    return video_destination.with_name(
        video_destination.stem
        + companion_suffix(companion_path)
    )



class FirstRunSetupDialog(QDialog):
    """First-launch setup for open-source Rogue Renamer."""

    TMDB_URL = "https://www.themoviedb.org/settings/api"
    TVDB_URL = "https://thetvdb.com/api-information"
    OMDB_URL = "https://www.omdbapi.com/apikey.aspx"
    FFMPEG_URL = "https://ffmpeg.org/download.html"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.config = load_config()
        self.selected_mode = "basic"

        self.setWindowTitle("Welcome to Rogue Renamer")
        self.resize(820, 760)
        self.setMinimumSize(700, 620)

        # The first-run dialog is created before the main Rogue window, so on
        # Windows it cannot rely on the main window's dark stylesheet. Give the
        # complete wizard an explicit Rogue palette so native light-theme
        # defaults never produce black-on-black or pale-on-white controls.
        self.setStyleSheet("""
            QDialog {
                background-color: #14171a;
                color: #eeeeee;
            }
            QWidget {
                color: #eeeeee;
            }
            QLabel {
                color: #eeeeee;
                background: transparent;
            }
            QScrollArea {
                background-color: #14171a;
                border: 1px solid #3f454b;
            }
            QScrollArea > QWidget > QWidget {
                background-color: #14171a;
            }
            QLineEdit {
                background-color: #202428;
                color: #ffffff;
                border: 1px solid #59616a;
                border-radius: 3px;
                padding: 7px;
                selection-background-color: #3b4654;
                selection-color: #ffffff;
            }
            QPushButton {
                background-color: #2a2f35;
                color: #f4f4f4;
                border: 1px solid #505861;
                border-radius: 4px;
                padding: 8px 12px;
            }
            QPushButton:hover {
                background-color: #353c43;
                border-color: #78838e;
            }
            QPushButton:pressed {
                background-color: #20252a;
            }
            QPushButton:disabled {
                background-color: #202428;
                color: #747b82;
                border-color: #383e44;
            }
            QRadioButton {
                color: #eeeeee;
                spacing: 8px;
                padding: 4px 0;
            }
            QRadioButton::indicator {
                width: 16px;
                height: 16px;
                border: 2px solid #8a949e;
                border-radius: 9px;
                background-color: #171a1d;
            }
            QRadioButton::indicator:hover {
                border-color: #c6d0da;
                background-color: #22272c;
            }
            QRadioButton::indicator:checked {
                border: 2px solid #9fd3ff;
                background-color: #1677c8;
            }
            QRadioButton::indicator:checked:hover {
                border-color: #d5ebff;
                background-color: #2389d7;
            }
            QScrollBar:vertical {
                background: #181c20;
                width: 12px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #59616a;
                min-height: 30px;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical:hover {
                background: #737d87;
            }
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0;
            }
        """)

        outer = QVBoxLayout(self)

        title = QLabel("Welcome to Rogue Renamer")
        title.setStyleSheet("font-size:28px; font-weight:bold;")
        outer.addWidget(title)

        subtitle = QLabel(
            "Let's set up your metadata providers. Your API credentials stay "
            "in Rogue Renamer's local configuration on this computer."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color:#c7cdd3; margin-bottom:8px;")
        outer.addWidget(subtitle)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            "QScrollArea { background:#14171a; border:none; } "
            "QScrollArea > QWidget > QWidget { background:#14171a; }"
        )
        body = QWidget()
        body.setStyleSheet("background:#14171a;")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(12, 8, 12, 8)

        self.tmdb_token = self._provider_section(
            layout,
            "TMDB",
            "General movie and TV metadata. Rogue can use the API Read Access Token.",
            "Get a TMDB API key / token",
            self.TMDB_URL,
            "API Read Access Token:",
            self.config.get("tmdb", {}).get("access_token", ""),
        )
        self.tmdb_status = QLabel("Not tested")
        layout.addWidget(self.tmdb_status)
        btn = QPushButton("Test TMDB")
        btn.clicked.connect(self.test_tmdb)
        layout.addWidget(btn)

        self.tvdb_key = self._provider_section(
            layout,
            "TheTVDB",
            "TV and episode metadata. A subscriber PIN is optional.",
            "Get a TheTVDB API key",
            self.TVDB_URL,
            "API Key:",
            self.config.get("tvdb", {}).get("api_key", ""),
        )
        tvdb_form = QFormLayout()
        self.tvdb_pin = QLineEdit()
        self.tvdb_pin.setEchoMode(QLineEdit.EchoMode.Password)
        self.tvdb_pin.setText(self.config.get("tvdb", {}).get("pin", ""))
        tvdb_form.addRow("PIN (optional):", self.tvdb_pin)
        layout.addLayout(tvdb_form)
        self.tvdb_status = QLabel("Not tested")
        layout.addWidget(self.tvdb_status)
        btn = QPushButton("Test TheTVDB")
        btn.clicked.connect(self.test_tvdb)
        layout.addWidget(btn)

        self.omdb_key = self._provider_section(
            layout,
            "OMDb",
            "An additional movie and TV metadata source.",
            "Get an OMDb API key",
            self.OMDB_URL,
            "API Key:",
            self.config.get("omdb", {}).get("api_key", ""),
        )
        self.omdb_status = QLabel("Not tested")
        layout.addWidget(self.omdb_status)
        btn = QPushButton("Test OMDb")
        btn.clicked.connect(self.test_omdb)
        layout.addWidget(btn)

        heading = QLabel("AniList")
        heading.setStyleSheet("font-size:19px; font-weight:bold; margin-top:16px;")
        layout.addWidget(heading)
        note = QLabel(
            "Anime metadata. No API key is required. AniList can be selected "
            "later from Rogue's metadata provider menu."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#c0c5ca;")
        layout.addWidget(note)
        self.anilist_status = QLabel("No API key required")
        layout.addWidget(self.anilist_status)
        btn = QPushButton("Test AniList")
        btn.clicked.connect(self.test_anilist)
        layout.addWidget(btn)

        heading = QLabel("FFmpeg — Optional but Recommended")
        heading.setStyleSheet("font-size:19px; font-weight:bold; margin-top:16px;")
        layout.addWidget(heading)
        ff = QLabel()
        ff.setWordWrap(True)
        ff.setTextFormat(Qt.TextFormat.RichText)
        ff.setOpenExternalLinks(True)
        ff.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        ff.setStyleSheet("QLabel a { color:#9fd3ff; font-weight:bold; text-decoration:underline; }")
        ff.setText(
            "FFmpeg gives Rogue deeper duplicate inspection using resolution, "
            "codec, bitrate, HDR, audio and other technical information. "
            "<a href='https://ffmpeg.org/download.html'>Install FFmpeg</a>"
        )
        layout.addWidget(ff)
        self.ffmpeg_status = QLabel()
        layout.addWidget(self.ffmpeg_status)
        self.refresh_ffmpeg_status()

        heading = QLabel("Choose Your Experience")
        heading.setStyleSheet("font-size:21px; font-weight:bold; margin-top:18px;")
        layout.addWidget(heading)

        self.basic_radio = QRadioButton(
            "Basic — simple rename-in-place workflow"
        )
        self.advanced_radio = QRadioButton(
            "Advanced — all Rogue Renamer features"
        )
        self.basic_radio.setChecked(True)
        layout.addWidget(self.basic_radio)
        layout.addWidget(self.advanced_radio)

        mode_help = QLabel(
            "You can switch modes later. Basic mode will keep media in its "
            "current folders; Advanced mode exposes library auditing, organization, "
            "history, automation and the rest of Rogue's tools."
        )
        mode_help.setWordWrap(True)
        mode_help.setStyleSheet("color:#c0c5ca;")
        layout.addWidget(mode_help)

        layout.addStretch()
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        footer = QHBoxLayout()
        skip = QPushButton("Skip for Now")
        skip.clicked.connect(self.reject)
        finish = QPushButton("Save & Finish Setup")
        finish.setDefault(True)
        finish.clicked.connect(self.finish_setup)
        footer.addWidget(skip)
        footer.addStretch()
        footer.addWidget(finish)
        outer.addLayout(footer)

    def _provider_section(
        self, layout, title, description, link_text, link_url,
        field_label, current_value
    ):
        heading = QLabel(title)
        heading.setStyleSheet("font-size:19px; font-weight:bold; margin-top:16px;")
        layout.addWidget(heading)

        help_label = QLabel(description)
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color:#c0c5ca;")
        layout.addWidget(help_label)

        link = QLabel(
            f"<a href='{link_url}'>{link_text}</a>"
        )
        link.setTextFormat(Qt.TextFormat.RichText)
        link.setOpenExternalLinks(True)
        link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        link.setStyleSheet(
            "QLabel a { color:#9fd3ff; font-weight:bold; text-decoration:underline; }"
        )
        layout.addWidget(link)

        form = QFormLayout()
        field = QLineEdit()
        field.setEchoMode(QLineEdit.EchoMode.Password)
        field.setText(current_value or "")
        form.addRow(field_label, field)
        layout.addLayout(form)
        return field

    def _save_entered_credentials(self):
        self.config["tmdb"] = dict(self.config.get("tmdb", {}))
        self.config["tmdb"]["access_token"] = self.tmdb_token.text().strip()

        self.config["tvdb"] = {
            "api_key": self.tvdb_key.text().strip(),
            "pin": self.tvdb_pin.text().strip(),
        }
        self.config["omdb"] = {
            "api_key": self.omdb_key.text().strip(),
        }
        save_config(self.config)

    def test_tmdb(self):
        token = self.tmdb_token.text().strip()
        if not token:
            self.tmdb_status.setText("❌ Enter a TMDB API Read Access Token.")
            return
        try:
            response = requests.get(
                "https://api.themoviedb.org/3/configuration",
                headers={"Authorization": f"Bearer {token}", "accept": "application/json"},
                timeout=10,
            )
            self.tmdb_status.setText(
                "✓ Connected to TMDB" if response.status_code == 200
                else "❌ TMDB connection failed"
            )
        except requests.RequestException:
            self.tmdb_status.setText("❌ Could not connect to TMDB")

    def test_tvdb(self):
        if not self.tvdb_key.text().strip():
            self.tvdb_status.setText("❌ Enter a TheTVDB API key.")
            return
        old = dict(self.config.get("tvdb", {}))
        self.config["tvdb"] = {
            "api_key": self.tvdb_key.text().strip(),
            "pin": self.tvdb_pin.text().strip(),
        }
        save_config(self.config)
        try:
            tvdb.test_connection()
            self.tvdb_status.setText("✓ Connected to TheTVDB")
        except tvdb.TVDBError as error:
            self.tvdb_status.setText(f"❌ {error}")
        finally:
            self.config["tvdb"] = old
            save_config(self.config)

    def test_omdb(self):
        if not self.omdb_key.text().strip():
            self.omdb_status.setText("❌ Enter an OMDb API key.")
            return
        old = dict(self.config.get("omdb", {}))
        self.config["omdb"] = {"api_key": self.omdb_key.text().strip()}
        save_config(self.config)
        try:
            omdb.test_connection()
            self.omdb_status.setText("✓ Connected to OMDb")
        except omdb.OMDbError as error:
            self.omdb_status.setText(f"❌ {error}")
        finally:
            self.config["omdb"] = old
            save_config(self.config)

    def test_anilist(self):
        try:
            anilist.test_connection()
            self.anilist_status.setText("✓ Connected to AniList")
        except anilist.AniListError as error:
            self.anilist_status.setText(f"❌ {error}")

    def refresh_ffmpeg_status(self):
        path = shutil.which("ffprobe")
        self.ffmpeg_status.setText(
            f"✓ FFmpeg / ffprobe detected: {path}"
            if path else
            "Not detected — Rogue will still work, but duplicate inspection is limited."
        )

    def finish_setup(self):
        self._save_entered_credentials()

        # Require one usable general provider before marking onboarding complete.
        has_general_provider = bool(
            self.tmdb_token.text().strip()
            or self.tvdb_key.text().strip()
            or self.omdb_key.text().strip()
        )
        if not has_general_provider:
            QMessageBox.warning(
                self,
                "Metadata Provider Required",
                "Configure at least one of TMDB, TheTVDB, or OMDb before finishing setup. "
                "AniList can still be used for anime.",
            )
            return

        self.selected_mode = (
            "advanced" if self.advanced_radio.isChecked() else "basic"
        )
        self.config.setdefault("ui", {})
        self.config["ui"]["setup_complete"] = True
        self.config["ui"]["preferred_mode"] = self.selected_mode
        save_config(self.config)
        self.accept()


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowTitle(
            "Rogue Renamer Settings"
        )

        self.resize(820, 860)

        self.config = load_config()

        # Make unchecked checkboxes clearly visible in the dark UI.
        self.setStyleSheet("""
            QCheckBox {
                spacing: 8px;
            }

            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border: 2px solid #8a949e;
                border-radius: 4px;
                background-color: #171a1d;
            }

            QCheckBox::indicator:hover {
                border: 2px solid #c6d0da;
                background-color: #22272c;
            }

            QCheckBox::indicator:checked {
                border: 2px solid #2f9df4;
                background-color: #1677c8;
            }

            QCheckBox::indicator:disabled {
                border: 2px solid #555d65;
                background-color: #202428;
            }
        """)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(8, 8, 8, 8)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)

        # QScrollArea creates its own viewport/content surface. Explicitly
        # keep those surfaces on Rogue's dark palette instead of allowing
        # the native Windows light background to show through.
        scroll.setStyleSheet("""
            QScrollArea {
                background-color: #14171a;
                border: none;
            }
            QScrollArea > QWidget > QWidget {
                background-color: #14171a;
            }
        """)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )

        scroll_content = QWidget()
        scroll_content.setObjectName("settingsScrollContent")
        scroll_content.setStyleSheet("""
            QWidget#settingsScrollContent {
                background-color: #14171a;
            }
            QWidget#settingsScrollContent QLabel {
                background-color: transparent;
            }
        """)
        layout = QVBoxLayout(scroll_content)
        layout.setContentsMargins(12, 12, 12, 12)

        scroll.setWidget(scroll_content)
        outer_layout.addWidget(scroll)

        title = QLabel(
            "Metadata Providers"
        )

        title.setStyleSheet(
            "font-size: 20px; "
            "font-weight: bold;"
        )

        layout.addWidget(title)

        form = QFormLayout()

        self.token_input = QLineEdit()

        self.token_input.setEchoMode(
            QLineEdit.EchoMode.Password
        )

        self.api_key_input = QLineEdit()

        self.api_key_input.setEchoMode(
            QLineEdit.EchoMode.Password
        )

        tmdb = self.config.get(
            "tmdb",
            {},
        )

        self.token_input.setText(
            tmdb.get(
                "access_token",
                "",
            )
        )

        self.api_key_input.setText(
            tmdb.get(
                "api_key",
                "",
            )
        )

        form.addRow(
            "API Read Access Token:",
            self.token_input,
        )

        form.addRow(
            "API Key:",
            self.api_key_input,
        )

        layout.addLayout(form)

        tvdb_title = QLabel("TheTVDB")
        tvdb_title.setStyleSheet(
            "font-size: 18px; font-weight: bold; margin-top: 12px;"
        )
        layout.addWidget(tvdb_title)

        tvdb_help = QLabel(
            "Enter your TheTVDB v4 project API key. PIN is optional and is only "
            "needed for user-supported keys that require a subscriber PIN."
        )
        tvdb_help.setWordWrap(True)
        tvdb_help.setStyleSheet("color: #aaaaaa;")
        layout.addWidget(tvdb_help)

        tvdb = self.config.get("tvdb", {})
        tvdb_form = QFormLayout()

        self.tvdb_api_key_input = QLineEdit()
        self.tvdb_api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.tvdb_api_key_input.setText(tvdb.get("api_key", ""))

        self.tvdb_pin_input = QLineEdit()
        self.tvdb_pin_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.tvdb_pin_input.setText(tvdb.get("pin", ""))

        tvdb_form.addRow("TVDB API Key:", self.tvdb_api_key_input)
        tvdb_form.addRow("TVDB PIN (optional):", self.tvdb_pin_input)
        layout.addLayout(tvdb_form)

        self.tvdb_connection_status = QLabel("TheTVDB connection not tested")
        layout.addWidget(self.tvdb_connection_status)

        tvdb_test_button = QPushButton("Test TheTVDB Connection")
        tvdb_test_button.clicked.connect(self.test_tvdb_connection)
        layout.addWidget(tvdb_test_button)

        omdb_title = QLabel("OMDb")
        omdb_title.setStyleSheet(
            "font-size: 18px; font-weight: bold; margin-top: 12px;"
        )
        layout.addWidget(omdb_title)

        omdb_help = QLabel(
            "Enter your activated OMDb API key. Rogue only queries OMDb "
            "when OMDb is selected as the metadata provider."
        )
        omdb_help.setWordWrap(True)
        omdb_help.setStyleSheet("color: #aaaaaa;")
        layout.addWidget(omdb_help)

        omdb_config = self.config.get("omdb", {})
        omdb_form = QFormLayout()
        self.omdb_api_key_input = QLineEdit()
        self.omdb_api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.omdb_api_key_input.setText(omdb_config.get("api_key", ""))
        omdb_form.addRow("OMDb API Key:", self.omdb_api_key_input)
        layout.addLayout(omdb_form)

        self.omdb_connection_status = QLabel("OMDb connection not tested")
        layout.addWidget(self.omdb_connection_status)

        omdb_test_button = QPushButton("Test OMDb Connection")
        omdb_test_button.clicked.connect(self.test_omdb_connection)
        layout.addWidget(omdb_test_button)

        anilist_title = QLabel("AniList")
        anilist_title.setStyleSheet(
            "font-size: 18px; font-weight: bold; margin-top: 12px;"
        )
        layout.addWidget(anilist_title)

        anilist_help = QLabel(
            "AniList provides public anime metadata without an API key. "
            "Rogue uses it only when AniList is selected as the metadata provider. "
            "AniList does not provide per-episode titles, so anime episodes use "
            "generic Episode 1 / Episode 2 titles."
        )
        anilist_help.setWordWrap(True)
        anilist_help.setStyleSheet("color: #aaaaaa;")
        layout.addWidget(anilist_help)

        self.anilist_connection_status = QLabel("AniList connection not tested")
        layout.addWidget(self.anilist_connection_status)

        anilist_test_button = QPushButton("Test AniList Connection")
        anilist_test_button.clicked.connect(self.test_anilist_connection)
        layout.addWidget(anilist_test_button)

        mode_title = QLabel("Interface")
        mode_title.setStyleSheet(
            "font-size: 20px; font-weight: bold; margin-top: 12px;"
        )
        layout.addWidget(mode_title)

        mode_help = QLabel(
            "Choose which interface Rogue opens by default. Basic keeps the "
            "rename workflow simple; Advanced exposes the full toolset."
        )
        mode_help.setWordWrap(True)
        mode_help.setStyleSheet("color: #aaaaaa;")
        layout.addWidget(mode_help)

        self.default_mode_combo = QComboBox()
        self.default_mode_combo.addItem("Basic", "basic")
        self.default_mode_combo.addItem("Advanced", "advanced")
        saved_mode = self.config.get("ui", {}).get("preferred_mode", "basic")
        mode_index = self.default_mode_combo.findData(saved_mode)
        if mode_index >= 0:
            self.default_mode_combo.setCurrentIndex(mode_index)
        layout.addWidget(self.default_mode_combo)

        rerun_setup_button = QPushButton("Run Setup Wizard Again")
        rerun_setup_button.clicked.connect(self.run_setup_again)
        layout.addWidget(rerun_setup_button)

        naming_title = QLabel("Naming Presets")
        naming_title.setStyleSheet(
            "font-size: 20px; font-weight: bold; margin-top: 12px;"
        )
        layout.addWidget(naming_title)

        naming_help = QLabel(
            "Movie fields: {title}, {year}, {tmdb_id}\n"
            "TV fields: {title}, {year}, {season}, {episode}, {episodes}, "
            "{episode_code}, {episode_title}, {tmdb_id}\n"
            "Formatting such as {season:02d} and {episode:02d} is supported."
        )
        naming_help.setWordWrap(True)
        naming_help.setStyleSheet("color: #aaaaaa;")
        layout.addWidget(naming_help)

        naming = self.config.get("naming", {})

        naming_form = QFormLayout()

        self.movie_template_input = QLineEdit()
        self.movie_template_input.setText(
            naming.get("movie_template", DEFAULT_MOVIE_TEMPLATE)
        )

        self.tv_template_input = QLineEdit()
        self.tv_template_input.setText(
            naming.get("tv_template", DEFAULT_TV_TEMPLATE)
        )

        naming_form.addRow("Movie template:", self.movie_template_input)
        naming_form.addRow("TV template:", self.tv_template_input)
        layout.addLayout(naming_form)

        organization_title = QLabel("Folder Organization")
        organization_title.setStyleSheet(
            "font-size: 20px; font-weight: bold; margin-top: 12px;"
        )
        layout.addWidget(organization_title)

        organization = self.config.get("organization", {})

        self.organize_checkbox = QCheckBox(
            "Organize matched media into folders"
        )
        self.organize_checkbox.setChecked(
            bool(organization.get("enabled", False))
        )
        layout.addWidget(self.organize_checkbox)

        organization_help = QLabel(
            "When enabled, Rogue Renamer can move matched media into separate "
            "Movies and TV library roots. Leave a root blank to keep the older "
            "behavior of organizing beneath the source folder. Use / in folder "
            "templates to create nested folders.\n"
            "Movie fields: {title}, {year}, {tmdb_id}\n"
            "TV fields: {title}, {year}, {season}, {episode}, {episodes}, "
            "{episode_code}, {episode_title}, {tmdb_id}"
        )
        organization_help.setWordWrap(True)
        organization_help.setStyleSheet("color: #aaaaaa;")
        layout.addWidget(organization_help)

        organization_form = QFormLayout()

        self.movie_library_root_input = QLineEdit()
        self.movie_library_root_input.setText(
            organization.get("movie_library_root", "")
        )
        self.movie_library_root_input.setPlaceholderText(
            r"Example: D:\Media\Movies"
        )

        movie_root_row = QHBoxLayout()
        movie_root_row.addWidget(self.movie_library_root_input, 1)
        movie_browse = QPushButton("Browse…")
        movie_browse.clicked.connect(
            lambda: self.choose_library_root(
                self.movie_library_root_input,
                "Select Movies Library",
            )
        )
        movie_root_row.addWidget(movie_browse)
        organization_form.addRow("Movies library:", movie_root_row)

        self.tv_library_root_input = QLineEdit()
        self.tv_library_root_input.setText(
            organization.get("tv_library_root", "")
        )
        self.tv_library_root_input.setPlaceholderText(
            r"Example: D:\Media\TV"
        )

        tv_root_row = QHBoxLayout()
        tv_root_row.addWidget(self.tv_library_root_input, 1)
        tv_browse = QPushButton("Browse…")
        tv_browse.clicked.connect(
            lambda: self.choose_library_root(
                self.tv_library_root_input,
                "Select TV Library",
            )
        )
        tv_root_row.addWidget(tv_browse)
        organization_form.addRow("TV library:", tv_root_row)

        self.movie_folder_input = QLineEdit()
        self.movie_folder_input.setText(
            organization.get(
                "movie_folder_template",
                DEFAULT_MOVIE_FOLDER_TEMPLATE,
            )
        )

        self.tv_folder_input = QLineEdit()
        self.tv_folder_input.setText(
            organization.get(
                "tv_folder_template",
                DEFAULT_TV_FOLDER_TEMPLATE,
            )
        )

        organization_form.addRow(
            "Movie folders:",
            self.movie_folder_input,
        )
        organization_form.addRow(
            "TV folders:",
            self.tv_folder_input,
        )
        layout.addLayout(organization_form)

        automation_title = QLabel("Batch Automation")
        automation_title.setStyleSheet(
            "font-size: 20px; font-weight: bold; margin-top: 12px;"
        )
        layout.addWidget(automation_title)

        automation = self.config.get("automation", {})

        automation_help = QLabel(
            "High-confidence matches at or above the threshold are accepted "
            "automatically. Ambiguous or lower-scoring matches stay queued for review."
        )
        automation_help.setWordWrap(True)
        automation_help.setStyleSheet("color: #aaaaaa;")
        layout.addWidget(automation_help)

        automation_form = QFormLayout()

        self.auto_accept_threshold = QSpinBox()
        self.auto_accept_threshold.setRange(50, 100)
        self.auto_accept_threshold.setSuffix(" / 100")
        self.auto_accept_threshold.setValue(
            int(automation.get("auto_accept_threshold", 90))
        )
        automation_form.addRow(
            "Auto-accept threshold:",
            self.auto_accept_threshold,
        )
        layout.addLayout(automation_form)

        self.review_all_checkbox = QCheckBox(
            "Review all matches (disable automatic acceptance)"
        )
        self.review_all_checkbox.setChecked(
            bool(automation.get("review_all_matches", False))
        )
        layout.addWidget(self.review_all_checkbox)

        self.connection_status = QLabel(
            "Connection not tested"
        )

        layout.addWidget(
            self.connection_status
        )

        buttons = QHBoxLayout()

        test_button = QPushButton(
            "Test Connection"
        )

        test_button.clicked.connect(
            self.test_connection
        )

        cancel_button = QPushButton(
            "Cancel"
        )

        cancel_button.clicked.connect(
            self.reject
        )

        save_button = QPushButton(
            "Save"
        )

        save_button.clicked.connect(
            self.save_settings
        )

        buttons.addWidget(test_button)
        buttons.addStretch()
        buttons.addWidget(cancel_button)
        buttons.addWidget(save_button)

        layout.addLayout(buttons)

    def run_setup_again(self):
        dialog = FirstRunSetupDialog(self)
        dialog.setStyleSheet(dialog.styleSheet())
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.config = load_config()
            mode = self.config.get("ui", {}).get("preferred_mode", "basic")
            index = self.default_mode_combo.findData(mode)
            if index >= 0:
                self.default_mode_combo.setCurrentIndex(index)

    def choose_library_root(self, line_edit, title):
        current = line_edit.text().strip()
        start = current if current and Path(current).exists() else ""

        folder = QFileDialog.getExistingDirectory(
            self,
            title,
            start,
        )
        if folder:
            line_edit.setText(folder)

    def test_connection(self):
        token = (
            self.token_input
            .text()
            .strip()
        )

        if not token:
            self.connection_status.setText(
                "❌ Enter your TMDB token."
            )
            return

        try:
            response = requests.get(
                "https://api.themoviedb.org/3/configuration",
                headers={
                    "Authorization":
                        f"Bearer {token}",
                    "accept":
                        "application/json",
                },
                timeout=10,
            )

            if response.status_code == 200:
                self.connection_status.setText(
                    "✓ Connected to TMDB successfully"
                )

            else:
                self.connection_status.setText(
                    "❌ TMDB connection failed"
                )

        except requests.RequestException:
            self.connection_status.setText(
                "❌ Could not connect to TMDB"
            )

    def test_tvdb_connection(self):
        key = self.tvdb_api_key_input.text().strip()
        pin = self.tvdb_pin_input.text().strip()

        if not key:
            self.tvdb_connection_status.setText("❌ Enter your TheTVDB API key.")
            return

        # Test the unsaved values without writing them permanently first.
        old_tvdb = dict(self.config.get("tvdb", {}))
        self.config["tvdb"] = {"api_key": key, "pin": pin}
        save_config(self.config)
        try:
            tvdb.test_connection()
            self.tvdb_connection_status.setText(
                "✓ Connected to TheTVDB successfully"
            )
        except tvdb.TVDBError as error:
            self.tvdb_connection_status.setText(f"❌ {error}")
        finally:
            self.config["tvdb"] = old_tvdb
            save_config(self.config)

    def test_omdb_connection(self):
        key = self.omdb_api_key_input.text().strip()

        if not key:
            self.omdb_connection_status.setText("❌ Enter your OMDb API key.")
            return

        old_omdb = dict(self.config.get("omdb", {}))
        self.config["omdb"] = {"api_key": key}
        save_config(self.config)
        try:
            omdb.test_connection()
            self.omdb_connection_status.setText(
                "✓ Connected to OMDb successfully"
            )
        except omdb.OMDbError as error:
            self.omdb_connection_status.setText(f"❌ {error}")
        finally:
            self.config["omdb"] = old_omdb
            save_config(self.config)

    def test_anilist_connection(self):
        try:
            anilist.test_connection()
            self.anilist_connection_status.setText(
                "✓ Connected to AniList successfully"
            )
        except anilist.AniListError as error:
            self.anilist_connection_status.setText(f"❌ {error}")

    def save_settings(self):
        self.config["tmdb"] = {
            "access_token":
                self.token_input
                .text()
                .strip(),

            "api_key":
                self.api_key_input
                .text()
                .strip(),
        }

        self.config["tvdb"] = {
            "api_key": self.tvdb_api_key_input.text().strip(),
            "pin": self.tvdb_pin_input.text().strip(),
        }

        self.config["omdb"] = {
            "api_key": self.omdb_api_key_input.text().strip(),
        }

        movie_template = (
            self.movie_template_input.text().strip()
            or DEFAULT_MOVIE_TEMPLATE
        )
        tv_template = (
            self.tv_template_input.text().strip()
            or DEFAULT_TV_TEMPLATE
        )

        try:
            render_naming_template(
                movie_template,
                {
                    "title": "Example Movie",
                    "year": 2026,
                    "tmdb_id": 12345,
                },
            )
            render_naming_template(
                tv_template,
                {
                    "title": "Example Show",
                    "year": 2026,
                    "season": 2,
                    "episode": 3,
                    "episodes": "03-04",
                    "episode_code": "S02E03-E04",
                    "episode_title": "Example Episode",
                    "tmdb_id": 67890,
                },
            )
        except ValueError as error:
            QMessageBox.warning(
                self,
                "Invalid Naming Template",
                str(error),
            )
            return

        self.config["naming"] = {
            "movie_template": movie_template,
            "tv_template": tv_template,
        }

        movie_folder_template = (
            self.movie_folder_input.text().strip()
            or DEFAULT_MOVIE_FOLDER_TEMPLATE
        )
        tv_folder_template = (
            self.tv_folder_input.text().strip()
            or DEFAULT_TV_FOLDER_TEMPLATE
        )

        try:
            render_folder_template(
                movie_folder_template,
                {
                    "title": "Example Movie",
                    "year": 2026,
                    "tmdb_id": 12345,
                },
            )
            render_folder_template(
                tv_folder_template,
                {
                    "title": "Example Show",
                    "year": 2026,
                    "season": 2,
                    "episode": 3,
                    "episodes": "03-04",
                    "episode_code": "S02E03-E04",
                    "episode_title": "Example Episode",
                    "tmdb_id": 67890,
                },
            )
        except ValueError as error:
            QMessageBox.warning(
                self,
                "Invalid Folder Template",
                str(error),
            )
            return

        self.config["organization"] = {
            "enabled": self.organize_checkbox.isChecked(),
            "movie_library_root": self.movie_library_root_input.text().strip(),
            "tv_library_root": self.tv_library_root_input.text().strip(),
            "movie_folder_template": movie_folder_template,
            "tv_folder_template": tv_folder_template,
        }

        self.config["automation"] = {
            "auto_accept_threshold": self.auto_accept_threshold.value(),
            "review_all_matches": self.review_all_checkbox.isChecked(),
        }

        self.config.setdefault("ui", {})
        self.config["ui"]["preferred_mode"] = (
            self.default_mode_combo.currentData() or "basic"
        )

        save_config(self.config)

        self.accept()


class MatchSelectionDialog(QDialog):
    def __init__(self, parsed, candidates, parent=None):
        super().__init__(parent)
        self.parsed = parsed
        self.candidates = candidates
        self.selected_match = None
        self.poster_cache = {}

        self.setWindowTitle(f"Choose Match — {parsed['title']}")
        self.resize(1040, 700)
        self.setMinimumSize(850, 580)

        layout = QVBoxLayout(self)

        heading = QLabel(f"Choose the correct match for “{parsed['title']}”")
        heading.setStyleSheet("font-size: 20px; font-weight: bold;")
        layout.addWidget(heading)

        requested = ""
        if parsed.get("type") == "TV":
            requested = f" • Requested: {format_episode_code(parsed)}"
        hints = []
        if parsed.get("year"):
            hints.append(f"year {parsed['year']}")
        if parsed.get("country_hint"):
            hints.append(f"country {parsed['country_hint']}")

        hint_text = (
            f" • Filename hints: {', '.join(hints)}"
            if hints
            else ""
        )

        explanation = QLabel(
            "Rogue Renamer found possible metadata matches"
            f"{requested}{hint_text}. "
            "Select a title to inspect its details."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        content = QHBoxLayout()

        self.list_widget = QListWidget()
        self.list_widget.setMinimumWidth(455)
        self.list_widget.setStyleSheet("""
            QListWidget { background:#1d2024; color:#eee; border:1px solid #444; padding:4px; }
            QListWidget::item { color:#eee; background:#1d2024; padding:12px 8px;
                                border-bottom:1px solid #30343a; }
            QListWidget::item:selected { color:#fff; background:#3b4654; }
            QListWidget::item:hover { background:#2b3036; }
        """)

        for candidate in candidates:
            year = candidate.get("year") or "Unknown year"
            score = candidate.get("score", 0)
            title = candidate.get("title", "Unknown")
            countries = candidate.get("origin_country") or []
            country_text = f" • {', '.join(countries)}" if countries else ""

            provider_name = candidate.get("provider_name") or get_active_provider_name()
            if candidate["type"] == "TV":
                state = (
                    format_episode_code(candidate)
                    if candidate.get("episode_title")
                    else "episode(s) missing"
                )
                detail = (
                    f"{title} ({year}){country_text}\n"
                    f"{provider_name} #{candidate.get('id', '?')}   •   {state}   •   Score {score}"
                )
            else:
                detail = (
                    f"{title} ({year}){country_text}\n"
                    f"{provider_name} #{candidate.get('id', '?')}   •   Score {score}"
                )

            item = QListWidgetItem(detail)
            item.setData(Qt.ItemDataRole.UserRole, candidate)
            self.list_widget.addItem(item)

        content.addWidget(self.list_widget, 5)

        details_frame = QFrame()
        details_frame.setStyleSheet("""
            QFrame { background:#1d2024; border:1px solid #444; }
            QLabel { border:none; background:transparent; }
        """)
        details_layout = QVBoxLayout(details_frame)

        top = QHBoxLayout()
        self.poster = QLabel("No poster")
        self.poster.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.poster.setFixedSize(180, 270)
        self.poster.setStyleSheet(
            "background:#15171a; border:1px solid #444; color:#888;"
        )
        top.addWidget(self.poster)

        meta_layout = QVBoxLayout()
        self.detail_title = QLabel()
        self.detail_title.setWordWrap(True)
        self.detail_title.setStyleSheet("font-size:20px; font-weight:bold;")
        meta_layout.addWidget(self.detail_title)

        self.detail_meta = QLabel()
        self.detail_meta.setWordWrap(True)
        self.detail_meta.setTextFormat(Qt.TextFormat.RichText)
        meta_layout.addWidget(self.detail_meta)

        self.episodes_label = QLabel()
        self.episodes_label.setWordWrap(True)
        self.episodes_label.setTextFormat(Qt.TextFormat.RichText)
        self.episodes_label.setStyleSheet("margin-top:8px;")
        meta_layout.addWidget(self.episodes_label)
        meta_layout.addStretch()
        top.addLayout(meta_layout, 1)
        details_layout.addLayout(top)

        overview_heading = QLabel("Overview")
        overview_heading.setStyleSheet("font-weight:bold; margin-top:8px;")
        details_layout.addWidget(overview_heading)

        self.overview = QTextEdit()
        self.overview.setReadOnly(True)
        self.overview.setStyleSheet("""
            QTextEdit { background:#15171a; color:#ddd; border:1px solid #444; padding:7px; }
        """)
        details_layout.addWidget(self.overview, 1)

        content.addWidget(details_frame, 6)
        layout.addLayout(content, 1)

        self.list_widget.currentItemChanged.connect(self.update_overview)
        self.list_widget.itemDoubleClicked.connect(lambda _item: self.use_selected())

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        use_button = QPushButton("Use Selected Match")
        use_button.clicked.connect(self.use_selected)
        use_button.setDefault(True)
        buttons.addWidget(cancel_button)
        buttons.addWidget(use_button)
        layout.addLayout(buttons)

        if self.list_widget.count():
            self.list_widget.setCurrentRow(0)

    def load_poster(self, candidate):
        poster_path = candidate.get("poster_path")
        poster_url = candidate.get("poster_url")
        poster_key = poster_url or poster_path
        if not poster_key:
            self.poster.setPixmap(QPixmap())
            self.poster.setText("No poster available")
            return

        pixmap = self.poster_cache.get(poster_key)
        if pixmap is None:
            self.poster.setPixmap(QPixmap())
            self.poster.setText("Loading poster…")
            QApplication.processEvents()
            try:
                response = requests.get(
                    (poster_url or f"https://image.tmdb.org/t/p/w342{poster_path}"),
                    timeout=8,
                )
                response.raise_for_status()
                pixmap = QPixmap()
                if not pixmap.loadFromData(QByteArray(response.content)):
                    pixmap = None
                if pixmap is not None:
                    self.poster_cache[poster_key] = pixmap
            except requests.RequestException:
                pixmap = None

        if pixmap is None:
            self.poster.setPixmap(QPixmap())
            self.poster.setText("Poster unavailable")
            return

        self.poster.setText("")
        self.poster.setPixmap(
            pixmap.scaled(
                self.poster.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def update_overview(self, current, previous):
        if not current:
            return

        candidate = current.data(Qt.ItemDataRole.UserRole)
        title = candidate.get("title", "Unknown")
        original_title = candidate.get("original_title") or ""
        year = candidate.get("year") or "Unknown"
        provider_id = candidate.get("id", "?")
        provider_name = candidate.get("provider_name") or get_active_provider_name()
        score = candidate.get("score", 0)
        language = (candidate.get("original_language") or "Unknown").upper()
        countries = candidate.get("origin_country") or []
        country_text = ", ".join(countries) if countries else "Unknown"

        self.detail_title.setText(f"{title} ({year})")

        date_value = (
            candidate.get("first_air_date")
            if candidate.get("type") == "TV"
            else candidate.get("release_date")
        ) or "Unknown"
        date_label = "First aired" if candidate.get("type") == "TV" else "Release date"

        original_line = ""
        if original_title and original_title.casefold() != title.casefold():
            original_line = f"<br><b>Original title:</b> {original_title}"

        score_reasons = candidate.get("score_reasons") or []
        score_breakdown = ""
        if score_reasons:
            score_breakdown = (
                "<br><b>Why:</b> "
                + " • ".join(score_reasons)
            )

        self.detail_meta.setText(
            f"<b>{provider_name} ID:</b> {provider_id}<br>"
            f"<b>{date_label}:</b> {date_value}<br>"
            f"<b>Country:</b> {country_text}<br>"
            f"<b>Original language:</b> {language}<br>"
            f"<b>Match score:</b> {score}/100"
            f"{score_breakdown}"
            f"{original_line}"
        )

        if candidate.get("type") == "TV":
            details = candidate.get("episode_details") or []
            lines = ["<b>Requested episodes:</b>"]
            if details:
                for ep in details:
                    number = ep.get("episode", 0)
                    name = ep.get("name") or "Untitled"
                    air = ep.get("air_date") or "Unknown air date"
                    lines.append(
                        f"S{candidate.get('season', 0):02d}E{number:02d} — "
                        f"{name} <span style='color:#aaa'>({air})</span>"
                    )
            else:
                for number in candidate.get("missing_episodes") or candidate.get("episodes") or []:
                    lines.append(
                        f"S{candidate.get('season', 0):02d}E{number:02d} — Not found"
                    )
            self.episodes_label.setText("<br>".join(lines))
        else:
            self.episodes_label.setText("<b>Type:</b> Movie")

        self.overview.setPlainText(
            candidate.get("overview") or "No description available."
        )
        self.load_poster(candidate)

    def use_selected(self):
        item = self.list_widget.currentItem()
        if not item:
            QMessageBox.warning(self, "Choose Match", "Select a match first.")
            return

        candidate = item.data(Qt.ItemDataRole.UserRole)
        if candidate["type"] == "TV" and not candidate.get("episode_title"):
            QMessageBox.warning(
                self,
                "Episode Not Found",
                "One or more requested episodes do not exist for this show and season.",
            )
            return

        self.selected_match = candidate
        self.accept()


class RenameConfirmationDialog(QDialog):
    def __init__(self, plan, parent=None):
        super().__init__(parent)

        self.setWindowTitle("Confirm Rename")
        self.resize(900, 600)
        self.setMinimumSize(650, 420)

        layout = QVBoxLayout(self)

        moving = any(
            item["source"].parent != item["destination"].parent
            for item in plan
        )
        action_word = "Move / Rename" if moving else "Rename"

        heading = QLabel(
            f"{action_word} {len(plan)} file"
            f"{'s' if len(plan) != 1 else ''}?"
        )
        heading.setStyleSheet(
            "font-size: 20px; font-weight: bold;"
        )
        layout.addWidget(heading)

        explanation = QLabel(
            "Review every source and destination below before continuing. "
            "Files may be moved into your configured library folders. "
            "No existing files will be overwritten."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(
            QTextEdit.LineWrapMode.NoWrap
        )
        self.preview.setStyleSheet(
            """
            QTextEdit {
                background: #1d2024;
                color: #eeeeee;
                border: 1px solid #444;
                padding: 8px;
            }
            """
        )

        preview_lines = []

        for index, item in enumerate(plan, start=1):
            if item.get("kind") == "companion":
                label = classify_companion(
                    item["source"] if "source" in item else item.get("old", "")
                ).upper().replace("-GLOBAL", "")
            else:
                label = "VIDEO"
            preview_lines.append(
                f"{index}. [{label}]\n"
                f"   FROM: {item['source']}\n"
                f"   TO:   {item['destination']}"
            )

        self.preview.setPlainText(
            "\n\n".join(preview_lines)
        )
        layout.addWidget(self.preview, 1)

        warning = QLabel(
            "No existing files will be overwritten."
        )
        warning.setStyleSheet(
            "color: #aaaaaa; font-weight: bold;"
        )
        layout.addWidget(warning)

        buttons = QHBoxLayout()
        buttons.addStretch()

        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)

        rename_button = QPushButton(
            "Move / Rename Files" if moving else "Rename Files"
        )
        rename_button.clicked.connect(self.accept)
        rename_button.setDefault(True)

        buttons.addWidget(cancel_button)
        buttons.addWidget(rename_button)
        layout.addLayout(buttons)


class RenameHistoryDialog(QDialog):
    def __init__(self, history, parent=None):
        super().__init__(parent)

        self.history = history
        self.selected_batch_id = None

        self.setWindowTitle("Rename History")
        self.resize(1050, 650)
        self.setMinimumSize(800, 500)

        layout = QVBoxLayout(self)

        heading = QLabel("Rename History")
        heading.setStyleSheet("font-size: 22px; font-weight: bold;")
        layout.addWidget(heading)

        explanation = QLabel(
            "Previous rename batches are stored locally. "
            "Select an active batch to review it or restore the original filenames."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        content = QHBoxLayout()

        self.batch_list = QListWidget()
        self.batch_list.setMinimumWidth(360)
        self.batch_list.setStyleSheet("""
            QListWidget {
                background: #1d2024;
                color: #eeeeee;
                border: 1px solid #444;
            }
            QListWidget::item {
                padding: 10px;
                border-bottom: 1px solid #30343a;
            }
            QListWidget::item:selected {
                background: #3b4654;
                color: #ffffff;
            }
        """)
        content.addWidget(self.batch_list, 4)

        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.details.setStyleSheet("""
            QTextEdit {
                background: #1d2024;
                color: #eeeeee;
                border: 1px solid #444;
                padding: 8px;
            }
        """)
        content.addWidget(self.details, 7)

        layout.addLayout(content, 1)

        buttons = QHBoxLayout()

        self.undo_button = QPushButton("Undo Selected Batch")
        self.undo_button.setEnabled(False)
        self.undo_button.clicked.connect(self.request_undo)

        close_button = QPushButton("Close")
        close_button.clicked.connect(self.reject)

        buttons.addWidget(self.undo_button)
        buttons.addStretch()
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self.batch_list.currentItemChanged.connect(
            self.update_details
        )

        self.populate()

    def populate(self):
        self.batch_list.clear()

        for batch in reversed(self.history):
            status = batch.get("status", "renamed")
            status_text = (
                "ACTIVE"
                if status == "renamed"
                else "UNDONE"
            )
            timestamp = batch.get("timestamp", "Unknown time")
            file_count = len(batch.get("items", []))
            video_count = sum(
                1
                for item in batch.get("items", [])
                if item.get("kind") == "video"
            )

            text = (
                f"{timestamp}\n"
                f"{status_text} • {video_count} video"
                f"{'s' if video_count != 1 else ''} • "
                f"{file_count} total file"
                f"{'s' if file_count != 1 else ''}"
            )

            item = QListWidgetItem(text)
            item.setData(
                Qt.ItemDataRole.UserRole,
                batch.get("id"),
            )
            self.batch_list.addItem(item)

        if self.batch_list.count():
            self.batch_list.setCurrentRow(0)
        else:
            self.details.setPlainText(
                "No rename history yet."
            )

    def update_details(self, current, previous):
        if not current:
            self.details.clear()
            self.undo_button.setEnabled(False)
            return

        batch_id = current.data(
            Qt.ItemDataRole.UserRole
        )
        batch = next(
            (
                entry
                for entry in self.history
                if entry.get("id") == batch_id
            ),
            None,
        )

        if not batch:
            self.details.clear()
            self.undo_button.setEnabled(False)
            return

        lines = [
            f"Date: {batch.get('timestamp', 'Unknown')}",
            f"Status: {batch.get('status', 'renamed').upper()}",
            "",
        ]

        for index, item in enumerate(
            batch.get("items", []),
            start=1,
        ):
            if item.get("kind") == "companion":
                label = classify_companion(
                    item["source"] if "source" in item else item.get("old", "")
                ).upper().replace("-GLOBAL", "")
            else:
                label = "VIDEO"
            lines.extend(
                [
                    f"{index}. [{label}]",
                    f"   Original: {item.get('old', '')}",
                    f"   Renamed:  {item.get('new', '')}",
                    "",
                ]
            )

        self.details.setPlainText("\n".join(lines))
        self.undo_button.setEnabled(
            batch.get("status") == "renamed"
        )

    def request_undo(self):
        item = self.batch_list.currentItem()
        if not item:
            return

        self.selected_batch_id = item.data(
            Qt.ItemDataRole.UserRole
        )
        self.accept()


def audit_library(root_path):
    """Read-only structural audit. Never renames, moves, creates, or deletes files."""
    root = Path(root_path)
    findings = []
    media_records = []
    media_by_directory = {}
    companion_files = []

    def add(severity, category, path, message):
        findings.append({
            "severity": severity,
            "category": category,
            "path": str(path),
            "message": message,
        })

    if not root.exists() or not root.is_dir():
        return [{
            "severity": "Problem",
            "category": "Library",
            "path": str(root),
            "message": "Selected library folder does not exist.",
        }]

    try:
        paths = sorted(
            (p for p in root.rglob("*") if p.is_file()),
            key=lambda p: str(p).casefold(),
        )
    except OSError as error:
        return [{
            "severity": "Problem",
            "category": "Library",
            "path": str(root),
            "message": f"Could not scan library: {error}",
        }]

    for path in paths:
        suffix = path.suffix.lower()

        if suffix in MEDIA_EXTENSIONS:
            if is_extra_video(path):
                add(
                    "OK",
                    "Extra",
                    path,
                    "Recognized as an extra/trailer and excluded from primary-media checks.",
                )
                continue

            parsed = parse_media_path(str(path))
            record = {"path": path, "parsed": parsed}
            media_records.append(record)
            media_by_directory.setdefault(path.parent, []).append(record)

            if parsed.get("type") == "Unknown":
                add(
                    "Problem",
                    "Unparseable Media",
                    path,
                    "Could not confidently identify this filename as a movie or TV episode.",
                )
                continue

            if parsed.get("type") == "TV":
                folder_season = parsed.get("folder_season")
                season = parsed.get("season")
                if folder_season is not None and season is not None and folder_season != season:
                    add(
                        "Problem",
                        "Season Conflict",
                        path,
                        f"Filename says Season {season:02d}, but the containing season folder says Season {folder_season:02d}.",
                    )

                folder_title = (parsed.get("folder_title") or "").strip()
                parsed_title = (parsed.get("title") or "").strip()

                # Generic library/container folders are not show titles.
                generic_library_folders = {
                    "tv", "tv show", "tv shows", "shows", "series",
                    "television", "media", "video", "videos",
                    "movie", "movies", "film", "films",
                }
                folder_title_is_generic = (
                    folder_title.casefold() in generic_library_folders
                    or (
                        folder_title
                        and Path(folder_title).name.casefold()
                        in generic_library_folders
                    )
                )

                if (
                    folder_title
                    and parsed_title
                    and not folder_title_is_generic
                    and folder_title.casefold() != parsed_title.casefold()
                ):
                    add(
                        "Warning",
                        "Title Conflict",
                        path,
                        f'Filename title "{parsed_title}" differs from folder title "{folder_title}".',
                    )

                if folder_season is None:
                    add(
                        "Warning",
                        "TV Structure",
                        path,
                        "TV episode is not inside a recognized Season/Series folder.",
                    )

            elif parsed.get("type") == "Movie":
                if not parsed.get("year"):
                    add(
                        "Warning",
                        "Movie Naming",
                        path,
                        "Movie was parsed without a year; matching may be ambiguous.",
                    )

        elif suffix in COMPANION_EXTENSIONS:
            companion_files.append(path)

    # Duplicate TV episode detection, including multi-episode files.
    episode_index = {}
    for record in media_records:
        parsed = record["parsed"]
        if parsed.get("type") != "TV":
            continue
        title = (parsed.get("title") or "").casefold()
        season = parsed.get("season")
        for episode in parsed.get("episodes") or [parsed.get("episode")]:
            if season is None or episode is None:
                continue
            key = (title, season, episode)
            episode_index.setdefault(key, []).append(record["path"])

    for (title, season, episode), paths_for_episode in episode_index.items():
        if len(paths_for_episode) > 1:
            display_paths = []
            for duplicate_path in paths_for_episode:
                try:
                    display_paths.append(str(duplicate_path.relative_to(root)))
                except ValueError:
                    display_paths.append(str(duplicate_path))
            names = " | ".join(display_paths)
            for path in paths_for_episode:
                add(
                    "Problem",
                    "Duplicate Episode",
                    path,
                    f"S{season:02d}E{episode:02d} appears in multiple primary video files: {names}",
                )

    # Sidecars are considered attached when their parsed base stem matches a
    # primary video stem in the same directory. Generic artwork is folder-level.
    for companion in companion_files:
        kind = classify_companion(companion)
        if kind == "artwork-global":
            add(
                "OK",
                "Folder Artwork",
                companion,
                "Recognized as generic folder/show artwork.",
            )
            continue

        base_stem, _tags = split_companion_filename(companion)
        directory_videos = media_by_directory.get(companion.parent, [])
        attached = any(
            record["path"].stem.casefold() == base_stem.casefold()
            for record in directory_videos
        )

        if not attached:
            add(
                "Warning",
                "Orphaned Sidecar",
                companion,
                f"{kind.replace('-', ' ').title()} does not match a primary video in the same folder.",
            )

    # Files with no finding are healthy primary media.
    paths_with_findings = {
        item["path"]
        for item in findings
        if item["severity"] in {"Warning", "Problem"}
    }
    for record in media_records:
        path = record["path"]
        if str(path) not in paths_with_findings:
            parsed = record["parsed"]
            if parsed.get("type") == "TV":
                code = format_episode_code(parsed)
                detail = f"Parsed successfully as {parsed.get('title', 'TV')} {code}."
            else:
                year = parsed.get("year") or "unknown year"
                detail = f"Parsed successfully as {parsed.get('title', 'Movie')} ({year})."
            add("OK", "Media", path, detail)

    severity_order = {"Problem": 0, "Warning": 1, "OK": 2}
    findings.sort(
        key=lambda item: (
            severity_order.get(item["severity"], 9),
            item["category"].casefold(),
            item["path"].casefold(),
        )
    )
    return findings



class LibraryMetadataCache:
    """Per-check metadata cache: one show match and one season fetch per key."""

    def __init__(self):
        self.matches = {}
        self.seasons = {}

    def match(self, parsed):
        key = (
            (parsed.get("title") or "").strip().casefold(),
            parsed.get("year"),
            (parsed.get("country_hint") or "").strip().casefold(),
        )
        if key not in self.matches:
            self.matches[key] = match_media(parsed)
        return self.matches[key]

    def season(self, series_id, season_number):
        key = (series_id, int(season_number))
        if key not in self.seasons:
            self.seasons[key] = get_tv_season(series_id, season_number)
        return self.seasons[key]


def build_library_inventory(root_path):
    """Walk and parse primary media once for Library Check."""
    root = Path(root_path)
    inventory = []
    companions = []

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in MEDIA_EXTENSIONS:
            inventory.append({
                "path": path,
                "is_extra": is_extra_video(path),
                "parsed": parse_media_path(str(path)),
            })
        elif suffix in COMPANION_EXTENSIONS:
            companions.append(path)

    inventory.sort(key=lambda item: str(item["path"]).casefold())
    companions.sort(key=lambda item: str(item).casefold())
    return inventory, companions

def add_tmdb_audit_findings(root_path, findings, inventory=None, metadata_cache=None):
    """Add read-only online metadata verification to structural audit results."""
    root = Path(root_path)
    metadata_cache = metadata_cache or LibraryMetadataCache()

    if inventory is None:
        inventory, _companions = build_library_inventory(root)

    groups = {}
    for item in inventory:
        if item.get("is_extra"):
            continue
        path = item["path"]
        parsed = item["parsed"]
        if parsed.get("type") != "TV":
            continue
        key = ((parsed.get("title") or "").casefold(),
               parsed.get("year"),
               (parsed.get("country_hint") or "").casefold())
        groups.setdefault(key, []).append({"path": path, "parsed": parsed})

    for records in groups.values():
        rep = records[0]
        parsed = rep["parsed"]
        try:
            match = metadata_cache.match(parsed)
        except TMDBError as error:
            findings.append({"severity":"Warning","category":"Metadata Audit",
                "path":str(rep["path"]),"message":f"Metadata verification could not be completed: {error}"})
            continue

        if not match:
            findings.append({"severity":"Problem","category":"Metadata Match",
                "path":str(rep["path"]),"message":f'No metadata match was found for "{parsed.get("title","Unknown")}".'})
            continue

        if match.get("confidence") == "Review":
            reason=match.get("ambiguity_reason") or "match requires review"
            findings.append({"severity":"Warning","category":"Metadata Match",
                "path":str(rep["path"]),
                "message":f'Metadata match for "{parsed.get("title","Unknown")}" is ambiguous: '
                          f'{match.get("title","Unknown")} ({match.get("year") or "unknown year"}), '
                          f'score {match.get("score",0)}/100 — {reason}. Gap checks skipped.'})
            continue

        tid=match.get("id"); title=match.get("title") or parsed.get("title") or "Unknown"
        if not tid: continue

        local={}; ep_paths={}
        for rec in records:
            p=rec["parsed"]; season=p.get("season")
            for ep in p.get("episodes") or [p.get("episode")]:
                if season is None or ep is None: continue
                local.setdefault(season,set()).add(ep)
                ep_paths.setdefault((season,ep),[]).append(rec["path"])

        for season, local_eps in sorted(local.items()):
            try:
                sd=metadata_cache.season(tid,season)
            except TMDBError as error:
                findings.append({"severity":"Warning","category":"Metadata Season",
                    "path":str(rep["path"]),"message":f"Could not verify {title} Season {season:02d}: {error}"})
                continue

            remote={int(e["episode_number"]):e for e in (sd.get("episodes") or [])
                    if e.get("episode_number") is not None}
            invalid=sorted(local_eps-set(remote))
            for ep in invalid:
                for path in ep_paths.get((season,ep),[rep["path"]]):
                    findings.append({"severity":"Problem","category":"Invalid Episode","path":str(path),
                        "message":f"{title} S{season:02d}E{ep:02d} does not exist in this metadata season."})

            # Only call gaps missing through the highest local episode. This avoids
            # flagging the uncollected remainder of a season.
            highest=max(local_eps) if local_eps else 0
            expected={n for n in remote if 1 <= n <= highest}
            missing=sorted(expected-local_eps)
            if missing:
                codes=", ".join(f"S{season:02d}E{ep:02d}" for ep in missing)
                findings.append({"severity":"Warning","category":"Missing Episode",
                    "path":str(rep["path"].parent),
                    "message":f"{title} Season {season:02d} has local episodes through E{highest:02d}, "
                              f"but {codes} {'is' if len(missing)==1 else 'are'} missing according to the selected metadata provider."})

            valid=len(local_eps & set(remote))
            if valid and not invalid:
                findings.append({"severity":"OK","category":"Metadata Verified",
                    "path":str(rep["path"].parent),
                    "message":f"{title} Season {season:02d}: {valid} local episode"
                              f"{'s' if valid != 1 else ''} verified against the selected metadata provider."})

    order={"Problem":0,"Warning":1,"OK":2}
    findings.sort(key=lambda x:(order.get(x["severity"],9),x["category"].casefold(),x["path"].casefold()))
    return findings



def collect_verified_tv_matches(root_path, inventory=None, metadata_cache=None):
    """Learn one safe High-confidence TV identity per parsed title.

    Performance rule: never metadata-match every episode in a library.
    Pick the strongest representative filename for each title and perform
    at most one match request per unique parsed title.
    """
    root = Path(root_path)
    metadata_cache = metadata_cache or LibraryMetadataCache()
    if inventory is None:
        inventory, _companions = build_library_inventory(root)

    best_by_title = {}

    for item in inventory:
        if item.get("is_extra"):
            continue
        path = item["path"]
        parsed = item["parsed"]
        if parsed.get("type") != "TV":
            continue

        title_key = (parsed.get("title") or "").strip().casefold()
        if not title_key:
            continue

        strength = (
            4 * int(bool(parsed.get("year")))
            + 2 * int(bool(parsed.get("country_hint")))
            + int(bool(parsed.get("folder_season")))
        )

        current = best_by_title.get(title_key)
        if current is None or strength > current[0]:
            best_by_title[title_key] = (strength, parsed)

    identities = {}
    for title_key, (_strength, parsed) in best_by_title.items():
        try:
            match = metadata_cache.match(parsed)
        except Exception:
            continue

        if match and match.get("confidence") == "High" and match.get("id"):
            identities[title_key] = match

    return identities


def build_audit_fix_plan(root_path, finding, verified_tv_matches=None, metadata_cache=None):
    """Build one conservative read-only suggested correction."""
    category=finding.get("category","")
    source=Path(finding.get("path",""))
    root=Path(root_path)
    plan={"available":False,"confidence":None,"current":str(source),
          "suggested":None,"reason":None}

    if not source.exists():
        plan["reason"] = f"Source path does not exist: {source}"
        return plan

    if not source.is_file():
        plan["reason"] = f"Selected finding is not a file: {source}"
        return plan

    parsed=parse_media_path(str(source))
    if parsed.get("type") not in {"TV","Movie"}:
        plan["reason"] = (
            f"Parser identified this file as {parsed.get('type', 'Unknown')!r}, "
            f"not TV or Movie. Parsed title: {parsed.get('title')!r}."
        )
        return plan

    supported_categories = {
        "TV Structure","Season Conflict","Title Conflict","Movie Naming",
        "Unparseable Media"
    }
    if category not in supported_categories:
        plan["reason"] = (
            f'Finding category "{category}" does not currently have a safe '
            "automatic repair."
        )
        return plan

    match = None
    reused_verified_identity = False

    # Audit repair may reuse a unique High-confidence identity learned from
    # stronger filenames elsewhere in the SAME audited tree. We still verify
    # the requested episode against that exact TMDB series before suggesting
    # any filesystem change.
    if parsed.get("type") == "TV" and verified_tv_matches:
        title_key = (parsed.get("title") or "").strip().casefold()
        verified = verified_tv_matches.get(title_key)

        if verified and verified.get("id"):
            season = parsed.get("season")
            episodes = parsed.get("episodes") or [parsed.get("episode")]

            if season is not None and all(ep is not None for ep in episodes):
                episode_details = []
                try:
                    if metadata_cache is not None:
                        season_data = metadata_cache.season(verified["id"], season)
                        season_index = {
                            int(item["episode_number"]): item
                            for item in (season_data.get("episodes") or [])
                            if item.get("episode_number") is not None
                        }
                    else:
                        season_index = {}

                    for ep in episodes:
                        data = season_index.get(int(ep)) if metadata_cache is not None else None
                        if data is None and metadata_cache is None:
                            data = get_tv_episode(verified["id"], season, ep)
                        if not data:
                            raise TMDBError(
                                f"S{season:02d}E{ep:02d} was not found for "
                                f"{verified.get('title', parsed.get('title', 'this series'))}."
                            )
                        episode_details.append({
                            "episode": ep,
                            "name": data.get("name") or f"Episode {ep}",
                            "air_date": data.get("air_date"),
                        })
                except TMDBError as error:
                    plan["reason"] = (
                        f"Verified series identity could not validate the requested "
                        f"episode: {error}"
                    )
                    return plan

                episode_title = " + ".join(
                    detail["name"] for detail in episode_details
                )
                match = dict(verified)
                match.update({
                    "type": "TV",
                    "season": season,
                    "episode": episodes[0],
                    "episodes": episodes,
                    "episode_title": episode_title,
                    "episode_details": episode_details,
                    "confidence": "High",
                    "score": 100,
                })
                reused_verified_identity = True

    if match is None:
        try:
            match=match_media(parsed)
        except TMDBError as error:
            plan["reason"]=f"TMDB verification unavailable: {error}"
            return plan
        except Exception as error:
            plan["reason"] = (
                f"Unexpected metadata error: {type(error).__name__}: {error}"
            )
            return plan

        if not match:
            plan["reason"]="No TMDB match was found."
            return plan

        if match.get("confidence")!="High":
            plan["reason"]=(
                f"Match confidence is {match.get('confidence','Unknown')} "
                f"({match.get('score',0)}/100); Rogue will not suggest a filesystem fix. "
                f"Run Verify with TMDB first so Rogue can reuse a verified series "
                f"identity from stronger filenames in this library."
            )
            return plan

    proposed_name=build_proposed_filename(source,match)
    org=get_organization_settings()

    if match["type"]=="Movie":
        values={"title":match.get("title",""),"year":match.get("year") or "",
                "tmdb_id":match.get("id") or ""}
        rel=render_folder_template(org["movie_folder_template"],values)
    else:
        values={"title":match.get("title",""),"year":match.get("year") or "",
                "season":match.get("season") or 0,"episode":match.get("episode") or 0,
                "episodes":format_episode_numbers(match),
                "episode_code":format_episode_code(match),
                "episode_title":match.get("episode_title") or "",
                "tmdb_id":match.get("id") or ""}
        rel=render_folder_template(org["tv_folder_template"],values)

    destination=root/rel/proposed_name
    if destination==source:
        plan["reason"]="The current path already matches Rogue's naming and folder templates."
        return plan

    plan.update({
        "available":True,
        "confidence":f"High ({match.get('score',0)}/100)",
        "suggested":str(destination),
        "match": match,
        "reason":(
            (
                "Reused verified library identity and confirmed the requested "
                "episode against TMDB: "
            )
            if reused_verified_identity else "TMDB verified as "
        )
        + f"{match.get('title',parsed.get('title','Unknown'))} "
          f"({match.get('year') or 'unknown year'}), TMDB #{match.get('id','?')}.",
    })
    return plan


class LibraryCheckWorker(QObject):
    finished = Signal(object, object, object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, root_path):
        super().__init__()
        self.root_path = str(root_path)

    def run(self):
        try:
            self.progress.emit("Scanning files locally…")
            inventory, _companions = build_library_inventory(self.root_path)
            findings = audit_library(self.root_path)

            cache = LibraryMetadataCache()
            self.progress.emit(
                f"Local scan complete — {len(inventory)} media files. Checking metadata…"
            )
            add_tmdb_audit_findings(
                self.root_path,
                findings,
                inventory=inventory,
                metadata_cache=cache,
            )

            self.progress.emit("Preparing safe fixes from cached metadata…")
            verified = collect_verified_tv_matches(
                self.root_path,
                inventory=inventory,
                metadata_cache=cache,
            )

            fix_plans = {}
            for finding in findings:
                if finding.get("severity") == "OK":
                    continue
                try:
                    plan = build_audit_fix_plan(
                        self.root_path,
                        finding,
                        verified,
                        metadata_cache=cache,
                    )
                except Exception as error:
                    plan = {
                        "available": False,
                        "reason": f"Could not prepare a repair: {error}",
                    }

                if plan.get("available"):
                    key = (
                        finding.get("category", ""),
                        finding.get("path", ""),
                        finding.get("message", ""),
                    )
                    fix_plans[key] = plan

            self.finished.emit(findings, verified, fix_plans)
        except Exception as error:
            self.failed.emit(f"{type(error).__name__}: {error}")


class LibraryAuditDialog(QDialog):
    """Simple library health/check UI over Rogue's existing audit/repair engine."""

    def __init__(self, root_path, findings, parent=None):
        super().__init__(parent)
        self.root_path = str(root_path)
        self.findings = findings
        self.verified_tv_matches = {}
        self.fix_plans = {}
        self.show_correct = False

        self.setWindowTitle("Rogue Renamer — Library Check")
        self.resize(1180, 760)
        self.setMinimumSize(860, 580)

        layout = QVBoxLayout(self)

        heading = QLabel("Check Library")
        heading.setStyleSheet("font-size:24px; font-weight:bold;")
        layout.addWidget(heading)

        self.summary_label = QLabel("Checking your library…")
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet(
            "font-size:16px; padding:10px; background:#1d2024; "
            "border:1px solid #444; border-radius:5px;"
        )
        layout.addWidget(self.summary_label)

        controls = QHBoxLayout()
        self.show_correct_checkbox = QCheckBox("Show correct files")
        self.show_correct_checkbox.setChecked(False)
        self.show_correct_checkbox.stateChanged.connect(self.toggle_correct_files)
        controls.addWidget(self.show_correct_checkbox)
        controls.addStretch()

        self.fix_button = QPushButton("Fix Selected")
        self.fix_button.setEnabled(False)
        self.fix_button.clicked.connect(self.apply_selected_fix)
        controls.addWidget(self.fix_button)

        close_button = QPushButton("Close")
        close_button.clicked.connect(self.reject)
        controls.addWidget(close_button)
        layout.addLayout(controls)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Status", "File", "Issue", "Action"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        # Keep the selected finding visually obvious across the entire row.
        # Some platform/theme combinations make the default selected-row
        # background almost indistinguishable from the table background.
        self.table.setStyleSheet("""
            QTableWidget {
                selection-background-color: #3b4654;
                selection-color: #ffffff;
            }
            QTableWidget::item:selected {
                background: #3b4654;
                color: #ffffff;
            }
        """)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.update_details)
        layout.addWidget(self.table, 3)

        detail_heading = QLabel("Details")
        detail_heading.setStyleSheet("font-size:17px; font-weight:bold;")
        layout.addWidget(detail_heading)

        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.details.setStyleSheet(
            "QTextEdit { background:#1d2024; color:#eee; border:1px solid #444; padding:8px; }"
        )
        layout.addWidget(self.details, 2)

        self.ffmpeg_notice = QLabel()
        self.ffmpeg_notice.setWordWrap(True)
        self.ffmpeg_notice.setTextFormat(Qt.TextFormat.RichText)
        self.ffmpeg_notice.setOpenExternalLinks(True)
        self.ffmpeg_notice.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        self.ffmpeg_notice.setStyleSheet(
            "QLabel { background:#20252a; color:#ddd; border:1px solid #555; "
            "padding:8px; } "
            "QLabel a { color:#9fd3ff; text-decoration:underline; font-weight:bold; }"
        )
        self.ffmpeg_notice.setText(
            "<b>Better duplicate inspection with FFmpeg</b><br>"
            "Install <a href='https://ffmpeg.org/download.html'>FFmpeg</a> "
            "to let Rogue compare resolution, codec, bitrate, HDR, audio tracks, "
            "and other technical details so it can recommend which duplicate "
            "appears better to keep. Rogue never automatically deletes duplicate media."
        )
        self.ffmpeg_notice.hide()
        layout.addWidget(self.ffmpeg_notice)

        self.worker_thread = None
        self.worker = None

        # Show the window first. Heavy filesystem/API work runs on a worker
        # thread so the GUI stays responsive.
        self.populate()
        self.start_background_check()

    def finding_key(self, finding):
        return (
            finding.get("category", ""),
            finding.get("path", ""),
            finding.get("message", ""),
        )

    def start_background_check(self):
        if self.worker_thread and self.worker_thread.isRunning():
            return

        self.fix_button.setEnabled(False)
        self.summary_label.setText("Checking library…")
        self.details.setPlainText(
            "Rogue is scanning the library in the background. "
            "You can move or resize this window while it works."
        )

        self.worker_thread = QThread(self)
        self.worker = LibraryCheckWorker(self.root_path)
        self.worker.moveToThread(self.worker_thread)

        self.worker_thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_check_progress)
        self.worker.finished.connect(self.on_check_finished)
        self.worker.failed.connect(self.on_check_failed)

        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.failed.connect(self.worker_thread.quit)
        self.worker_thread.finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker_thread.finished.connect(self.clear_worker_refs)

        self.worker_thread.start()

    def clear_worker_refs(self):
        self.worker = None
        self.worker_thread = None

    def on_check_progress(self, message):
        self.summary_label.setText(message)

    def on_check_finished(self, findings, verified, fix_plans):
        self.findings = findings
        self.verified_tv_matches = verified
        self.fix_plans = fix_plans
        self.populate()

    def on_check_failed(self, message):
        self.summary_label.setText("Library check could not be completed.")
        self.details.setPlainText(message)
        self.fix_button.setEnabled(False)

    def classify_finding(self, finding):
        if finding.get("severity") == "OK":
            return "Correct", "—"

        plan = self.fix_plans.get(self.finding_key(finding))
        if plan and plan.get("available"):
            return "Safe to Fix", "Fix"

        category = finding.get("category", "")
        if category == "Missing Episode":
            return "Missing", "—"
        return "Needs Review", "Review"

    def visible_findings(self):
        if self.show_correct:
            return list(self.findings)
        return [f for f in self.findings if f.get("severity") != "OK"]

    def update_summary(self):
        safe = review = missing = correct = 0
        for finding in self.findings:
            status, _action = self.classify_finding(finding)
            if status == "Safe to Fix":
                safe += 1
            elif status == "Missing":
                missing += 1
            elif status == "Correct":
                correct += 1
            else:
                review += 1

        provider = get_active_provider_name()
        self.summary_label.setText(
            f"Library Check Complete — {safe} safe fix"
            f"{'es' if safe != 1 else ''} • "
            f"{review} need review • {missing} missing • "
            f"{correct} correct\nMetadata provider: {provider}"
        )

    def populate(self, selected_key=None):
        rows = self.visible_findings()
        self.table.setRowCount(len(rows))
        selected_row = None

        for row, finding in enumerate(rows):
            status, action = self.classify_finding(finding)
            path = Path(finding.get("path", ""))
            try:
                display_path = str(path.relative_to(Path(self.root_path)))
            except ValueError:
                display_path = str(path)

            status_item = QTableWidgetItem(status)
            status_item.setData(Qt.ItemDataRole.UserRole, finding)
            self.table.setItem(row, 0, status_item)
            self.table.setItem(row, 1, QTableWidgetItem(display_path))
            self.table.setItem(row, 2, QTableWidgetItem(finding.get("message", "")))
            self.table.setItem(row, 3, QTableWidgetItem(action))

            if selected_key and self.finding_key(finding) == selected_key:
                selected_row = row

        self.update_summary()

        if self.table.rowCount():
            row = selected_row if selected_row is not None else 0
            self.table.selectRow(row)
            item = self.table.item(row, 0)
            if item:
                self.table.scrollToItem(item)
        else:
            self.details.setPlainText(
                "No issues were found. Your library looks good."
            )
            self.fix_button.setEnabled(False)

    def toggle_correct_files(self, state):
        selected = self.current_finding()
        selected_key = self.finding_key(selected) if selected else None
        self.show_correct = bool(state)
        self.populate(selected_key)

    def current_finding(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def inspect_media_file(self, file_path):
        """Inspect a duplicate locally. Uses ffprobe when available."""
        path = Path(file_path)
        info = {
            "path": str(path),
            "exists": path.exists(),
            "size": path.stat().st_size if path.exists() else 0,
            "width": None,
            "height": None,
            "codec": None,
            "bitrate": None,
            "duration": None,
            "audio": [],
            "hdr": False,
            "probe_available": False,
        }

        ffprobe = shutil.which("ffprobe")
        if not ffprobe or not path.exists():
            return info

        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v", "error",
                    "-show_entries",
                    "format=duration,bit_rate:"
                    "stream=index,codec_type,codec_name,width,height,bit_rate,"
                    "channels,channel_layout,color_transfer,color_primaries,"
                    "color_space",
                    "-of", "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                return info

            data = json.loads(result.stdout or "{}")
            info["probe_available"] = True
            fmt = data.get("format") or {}

            try:
                info["duration"] = float(fmt.get("duration")) if fmt.get("duration") else None
            except (TypeError, ValueError):
                pass

            try:
                info["bitrate"] = int(fmt.get("bit_rate")) if fmt.get("bit_rate") else None
            except (TypeError, ValueError):
                pass

            for stream in data.get("streams") or []:
                if stream.get("codec_type") == "video" and info["width"] is None:
                    info["width"] = stream.get("width")
                    info["height"] = stream.get("height")
                    info["codec"] = (stream.get("codec_name") or "").upper() or None

                    transfer = (stream.get("color_transfer") or "").lower()
                    primaries = (stream.get("color_primaries") or "").lower()
                    info["hdr"] = (
                        transfer in {"smpte2084", "arib-std-b67"}
                        or "2020" in primaries
                    )

                    if not info["bitrate"]:
                        try:
                            info["bitrate"] = int(stream.get("bit_rate"))
                        except (TypeError, ValueError):
                            pass

                elif stream.get("codec_type") == "audio":
                    codec = (stream.get("codec_name") or "").upper()
                    channels = stream.get("channels")
                    layout = stream.get("channel_layout")
                    label = codec or "Audio"
                    if layout:
                        label += f" {layout}"
                    elif channels:
                        label += f" {channels}ch"
                    info["audio"].append(label)

        except Exception:
            pass

        return info

    def format_bytes(self, value):
        if not value:
            return "Unknown"
        size = float(value)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
            size /= 1024

    def format_bitrate(self, value):
        if not value:
            return "Unknown"
        return f"{value / 1_000_000:.1f} Mbps"

    def duplicate_quality_score(self, info):
        """Conservative quality ranking. Recommendation only."""
        score = 0.0
        pixels = (info.get("width") or 0) * (info.get("height") or 0)
        score += pixels / 1_000_000 * 100

        # Prefer efficient modern codecs slightly, but resolution/bitrate/size
        # remain the dominant signals.
        codec = (info.get("codec") or "").lower()
        codec_bonus = {
            "av1": 20,
            "hevc": 15,
            "h265": 15,
            "vp9": 10,
            "h264": 5,
            "avc": 5,
        }
        score += codec_bonus.get(codec, 0)

        if info.get("hdr"):
            score += 20

        bitrate = info.get("bitrate") or 0
        score += min(bitrate / 1_000_000, 50)

        audio = info.get("audio") or []
        score += min(len(audio), 5) * 2

        # Size is only a weak tiebreaker; larger does not automatically mean better.
        size_gb = (info.get("size") or 0) / (1024 ** 3)
        score += min(size_gb, 20) * 0.5
        return score

    def format_duplicate_details(self, finding):
        message = finding.get("message", "")
        episode_code = "Episode"
        match = re.search(r"\bS\d{2}E\d{2}(?:-E\d{2})?\b", message, re.IGNORECASE)
        if match:
            episode_code = match.group(0).upper()

        locations = []
        if "multiple primary video files:" in message:
            raw = message.split("multiple primary video files:", 1)[1].strip()
            for item in raw.split(" | "):
                item = item.strip()
                if not item:
                    continue
                path = Path(item)
                if not path.is_absolute():
                    path = Path(self.root_path) / path
                locations.append(str(path))

        inspections = [self.inspect_media_file(location) for location in locations]

        lines = [
            f"DUPLICATE EPISODE — {episode_code}",
            "",
            f"Rogue found {len(locations) if locations else 'multiple'} video files "
            "that appear to represent the same episode.",
        ]

        if inspections:
            lines.extend(["", "COPIES"])
            for number, info in enumerate(inspections, 1):
                resolution = (
                    f"{info['width']}x{info['height']}"
                    if info.get("width") and info.get("height")
                    else "Unknown resolution"
                )
                codec = info.get("codec") or "Unknown codec"
                hdr = " • HDR" if info.get("hdr") else ""
                audio = ", ".join(info.get("audio") or []) or "Unknown audio"

                lines.extend([
                    "",
                    f"{number}. {info['path']}",
                    f"   Video: {resolution} • {codec}{hdr}",
                    f"   Bitrate: {self.format_bitrate(info.get('bitrate'))}",
                    f"   Audio: {audio}",
                    f"   Size: {self.format_bytes(info.get('size'))}",
                ])

        if inspections and any(item.get("probe_available") for item in inspections):
            ranked = sorted(
                enumerate(inspections, 1),
                key=lambda pair: self.duplicate_quality_score(pair[1]),
                reverse=True,
            )
            best_number, best = ranked[0]

            lines.extend([
                "",
                "ROGUE RECOMMENDATION",
                f"Copy {best_number} appears to be the best one to keep based on "
                "resolution, codec, bitrate, HDR, audio tracks, and file size.",
                "",
                f"Recommended: {best['path']}",
            ])

            if len(ranked) > 1:
                top_score = self.duplicate_quality_score(ranked[0][1])
                second_score = self.duplicate_quality_score(ranked[1][1])
                if abs(top_score - second_score) < 10:
                    lines.extend([
                        "",
                        "The copies are technically very similar, so Rogue's "
                        "recommendation is low confidence. Compare them manually "
                        "before removing anything.",
                    ])
        else:
            lines.extend([
                "",
                "ROGUE RECOMMENDATION",
                "Detailed video inspection is unavailable because ffprobe was not "
                "found. Rogue can still show file sizes, but cannot reliably "
                "recommend which encode is better. Use the FFmpeg installation "
                "link below to enable technical comparison.",
            ])

        lines.extend([
            "",
            "SAFETY",
            "Rogue will not automatically delete duplicate media. "
            "The recommendation is informational only; you decide which copy to remove.",
        ])
        return "\n".join(lines)

    def update_details(self):
        finding = self.current_finding()
        self.fix_button.setEnabled(False)
        self.ffmpeg_notice.hide()

        if not finding:
            self.details.clear()
            return

        status, action = self.classify_finding(finding)
        plan = self.fix_plans.get(self.finding_key(finding))
        provider = get_active_provider_name()

        if finding.get("category") == "Duplicate Episode":
            self.details.setPlainText(self.format_duplicate_details(finding))
            self.ffmpeg_notice.setVisible(shutil.which("ffprobe") is None)
            return

        lines = [
            f"STATUS: {status}",
            f"ISSUE: {finding.get('category', 'Unknown')}",
            f"FILE: {finding.get('path', '')}",
            "",
            finding.get("message", ""),
        ]

        if plan and plan.get("available"):
            lines.extend([
                "",
                "ROGUE CAN FIX THIS SAFELY",
                f"Metadata: {provider} • {plan.get('confidence') or 'High confidence'}",
                "",
                "CURRENT",
                str(plan.get("current") or finding.get("path", "")),
                "",
                "ROGUE SUGGESTS",
                str(plan.get("suggested") or ""),
                "",
                plan.get("reason") or "",
            ])
            self.fix_button.setEnabled(True)
        elif status == "Missing":
            lines.extend([
                "",
                "Rogue found a gap in the local episode sequence. "
                "No file will be created, moved or deleted automatically.",
            ])
        elif status == "Correct":
            lines.extend(["", "No action is needed."])
        else:
            lines.extend([
                "",
                "This needs review. Rogue will not make an automatic filesystem "
                "decision for this item.",
            ])

        self.details.setPlainText("\n".join(lines))

    def apply_selected_fix(self):
        finding = self.current_finding()
        if not finding:
            return

        key = self.finding_key(finding)
        plan = self.fix_plans.get(key)
        if not plan or not plan.get("available"):
            QMessageBox.information(
                self,
                "Fix Selected",
                "This item does not have a safe automatic repair.",
            )
            return

        parent = self.parent()
        if not parent or not hasattr(parent, "apply_audit_fix"):
            QMessageBox.critical(
                self,
                "Fix Selected",
                "The main Rogue Renamer window is unavailable.",
            )
            return

        if not parent.apply_audit_fix(plan, finding):
            return

        # Re-run in the background so the UI remains responsive.
        self.findings = []
        self.verified_tv_matches = {}
        self.fix_plans = {}
        self.populate()
        self.start_background_check()

        QMessageBox.information(
            self,
            "Library Repair Complete",
            "The selected repair was completed and added to Rename History. "
            "You can restore it with Undo/History.",
        )


class RogueRenamer(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle(
            "Rogue Renamer"
        )

        self.resize(1500, 760)

        self.loaded_files = set()
        self.rename_history = load_rename_history()
        self.last_rename_batch = self.get_latest_active_batch_items()

        self.setAcceptDrops(True)

        central = QWidget()

        self.setCentralWidget(
            central
        )

        layout = QVBoxLayout(
            central
        )

        # HEADER

        header = QHBoxLayout()

        titles = QVBoxLayout()

        app_title = QLabel(
            "ROGUE RENAMER"
        )

        app_title.setStyleSheet(
            "font-size: 30px; "
            "font-weight: bold;"
        )

        subtitle = QLabel(
            "Movie & TV metadata renaming"
        )

        titles.addWidget(app_title)
        titles.addWidget(subtitle)

        settings_button = QPushButton(
            "⚙ Settings"
        )

        settings_button.clicked.connect(
            self.open_settings
        )

        self.history_button = QPushButton(
            "History"
        )
        self.history_button.clicked.connect(
            self.open_history
        )

        self.mode_button = QPushButton()
        self.mode_button.clicked.connect(self.toggle_mode)

        header.addLayout(titles)
        header.addStretch()
        header.addWidget(self.mode_button)
        header.addWidget(
            self.history_button
        )
        header.addWidget(
            settings_button
        )

        layout.addLayout(header)

        # CONTROLS

        controls = QHBoxLayout()

        folder_button = QPushButton(
            "Select Folder"
        )

        folder_button.clicked.connect(
            self.select_folder
        )

        files_button = QPushButton(
            "Select Files"
        )

        files_button.clicked.connect(
            self.select_files
        )

        clear_button = QPushButton(
            "Clear"
        )

        clear_button.clicked.connect(
            self.clear_files
        )

        controls.addWidget(
            folder_button
        )

        controls.addWidget(
            files_button
        )

        controls.addWidget(
            clear_button
        )

        self.scan_subfolders_checkbox = QCheckBox(
            "Scan Subfolders"
        )
        self.scan_subfolders_checkbox.setChecked(True)
        self.scan_subfolders_checkbox.setToolTip(
            "When enabled, folder scans include media inside all nested folders."
        )
        controls.addWidget(
            self.scan_subfolders_checkbox
        )

        provider_label = QLabel("Metadata:")
        controls.addWidget(provider_label)

        self.provider_combo = QComboBox()

        # Keep both the closed provider selector and its popup consistent
        # with Rogue's dark theme. On Windows the popup view otherwise
        # inherits the native light palette.
        self.provider_combo.setStyleSheet("""
            QComboBox {
                background-color: #24292f;
                color: #ffffff;
                border: 1px solid #4a5057;
                border-radius: 4px;
                padding: 5px 28px 5px 8px;
            }
            QComboBox:hover {
                border: 1px solid #6a727b;
            }
            QComboBox:focus {
                border: 1px solid #7d8792;
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 24px;
                border-left: 1px solid #4a5057;
                background-color: #2b3036;
            }
            QComboBox QAbstractItemView {
                background-color: #24292f;
                color: #ffffff;
                border: 1px solid #4a5057;
                selection-background-color: #3b4654;
                selection-color: #ffffff;
                outline: 0;
            }
        """)
        self.provider_combo.setToolTip(
            "Choose the metadata database Rogue Renamer will use. "
            "Only the selected provider is queried."
        )
        for provider in available_providers():
            self.provider_combo.addItem(provider["name"], provider["id"])

        active_provider = get_active_provider_id()
        active_index = self.provider_combo.findData(active_provider)
        if active_index >= 0:
            self.provider_combo.setCurrentIndex(active_index)

        self.provider_combo.currentIndexChanged.connect(
            self.metadata_provider_changed
        )
        controls.addWidget(self.provider_combo)

        self.audit_button = QPushButton("Check Library")
        self.audit_button.setToolTip(
            "Scan an existing Movies or TV library for structural problems without changing files."
        )
        self.audit_button.clicked.connect(self.open_library_audit)
        controls.addWidget(self.audit_button)

        controls.addStretch()

        layout.addLayout(controls)

        # DROP AREA

        drop = QLabel(
            "Drop movies, TV episodes, "
            "or folders here"
        )

        drop.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )

        drop.setMinimumHeight(60)

        drop.setStyleSheet(
            "border: 2px dashed #555;"
            "color: #aaa;"
        )

        layout.addWidget(drop)

        # TABLE

        self.table = QTableWidget(
            0,
            8,
        )

        self.table.setHorizontalHeaderLabels(
            [
                "Original",
                "Type",
                "Parsed Title",
                "S/E",
                "Metadata Match",
                "Episode Title",
                "Proposed Filename",
                "Status",
            ]
        )

        # Main file list: selecting any cell selects and highlights the full row.
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        self.table.setStyleSheet("""
            QTableWidget {
                selection-background-color: #3b4654;
                selection-color: #ffffff;
            }
            QTableWidget::item:selected {
                background-color: #3b4654;
                color: #ffffff;
            }
        """)

        # Responsive table layout. Fixed-width utility columns stay compact,
        # while text-heavy columns share whatever width the window provides.
        header_view = self.table.horizontalHeader()

        header_view.setSectionResizeMode(
            0,
            QHeaderView.ResizeMode.Stretch,
        )
        header_view.setSectionResizeMode(
            1,
            QHeaderView.ResizeMode.Fixed,
        )
        header_view.setSectionResizeMode(
            2,
            QHeaderView.ResizeMode.Stretch,
        )
        header_view.setSectionResizeMode(
            3,
            QHeaderView.ResizeMode.Fixed,
        )
        header_view.setSectionResizeMode(
            4,
            QHeaderView.ResizeMode.Stretch,
        )
        header_view.setSectionResizeMode(
            5,
            QHeaderView.ResizeMode.Stretch,
        )
        header_view.setSectionResizeMode(
            6,
            QHeaderView.ResizeMode.Stretch,
        )
        header_view.setSectionResizeMode(
            7,
            QHeaderView.ResizeMode.Stretch,
        )

        self.table.setColumnWidth(1, 72)
        self.table.setColumnWidth(3, 82)

        # Keep the table usable at unusually small window sizes too.
        self.table.setHorizontalScrollMode(
            QTableWidget.ScrollMode.ScrollPerPixel
        )
        self.table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )

        header_view.setMinimumSectionSize(55)
        header_view.setStretchLastSection(False)

        self.table.cellDoubleClicked.connect(
            self.review_match
        )

        layout.addWidget(
            self.table
        )

        # FOOTER

        footer = QHBoxLayout()

        self.status_label = QLabel(
            "0 files loaded"
        )

        self.search_button = QPushButton(
            "Search Metadata"
        )

        self.search_button.setEnabled(
            False
        )

        self.search_button.clicked.connect(
            self.search_metadata
        )

        self.review_button = QPushButton(
            "Review Matches"
        )
        self.review_button.setEnabled(False)
        self.review_button.clicked.connect(
            self.review_next_match
        )

        self.rename_button = QPushButton(
            "Rename Files"
        )

        self.rename_button.setEnabled(
            False
        )
        self.rename_button.clicked.connect(
            self.rename_files
        )

        for button in (
            self.search_button,
            self.review_button,
            self.rename_button,
        ):
            button.setMinimumWidth(0)

        self.undo_button = QPushButton(
            "Undo Last Rename"
        )
        self.undo_button.setEnabled(
            bool(self.last_rename_batch)
        )
        self.undo_button.clicked.connect(
            self.undo_last_rename
        )

        self.undo_button.setMinimumWidth(0)

        footer.addWidget(
            self.status_label
        )

        footer.addStretch()

        footer.addWidget(
            self.search_button
        )

        footer.addWidget(
            self.review_button
        )

        footer.addWidget(
            self.undo_button
        )

        footer.addWidget(
            self.rename_button
        )

        layout.addLayout(
            footer
        )

        self.apply_styles()

        self.current_mode = (
            load_config().get("ui", {}).get("preferred_mode", "basic") or "basic"
        )
        self.apply_interface_mode()

    def apply_styles(self):
        self.setStyleSheet(
            """
            QMainWindow, QDialog {
                background: #15171a;
            }

            QWidget {
                color: #eeeeee;
                font-size: 14px;
            }

            QLineEdit {
                background: #1d2024;
                border: 1px solid #444;
                padding: 7px;
            }

            QPushButton {
                background: #292d32;
                border: 1px solid #444;
                padding: 9px 18px;
                border-radius: 4px;
            }

            QPushButton:hover {
                background: #373c42;
            }

            QPushButton:disabled {
                color: #666;
                background: #202225;
            }

            QTableWidget {
                background: #1d2024;
                alternate-background-color: #22262b;
                gridline-color: #34383d;
            }

            QHeaderView::section {
                background: #292d32;
                padding: 8px;
                font-weight: bold;
            }
            """
        )

    def get_latest_active_batch(self):
        for batch in reversed(self.rename_history):
            if batch.get("status") == "renamed":
                return batch
        return None

    def get_latest_active_batch_items(self):
        batch = self.get_latest_active_batch()
        if not batch:
            return []

        return [
            {
                "row": item.get("row"),
                "old": Path(item["old"]),
                "new": Path(item["new"]),
                "kind": item.get("kind", "video"),
                "created_directories": [
                    Path(path)
                    for path in item.get(
                        "created_directories",
                        [],
                    )
                ],
            }
            for item in batch.get("items", [])
        ]

    def save_completed_batch(self, completed):
        batch = {
            "id": uuid.uuid4().hex,
            "timestamp": datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
            "status": "renamed",
            "items": [
                {
                    "row": item.get("row"),
                    "old": str(item["old"]),
                    "new": str(item["new"]),
                    "kind": item.get("kind", "video"),
                    "created_directories": [
                        str(path)
                        for path in item.get(
                            "created_directories",
                            [],
                        )
                    ],
                }
                for item in completed
            ],
        }

        self.rename_history.append(batch)

        # Keep history useful without allowing the file to grow forever.
        if len(self.rename_history) > 500:
            self.rename_history = self.rename_history[-500:]

        save_rename_history(self.rename_history)
        return batch

    def mark_batch_undone(self, batch_id):
        for batch in self.rename_history:
            if batch.get("id") == batch_id:
                batch["status"] = "undone"
                batch["undone_at"] = (
                    datetime.now()
                    .astimezone()
                    .isoformat(timespec="seconds")
                )
                break

        save_rename_history(self.rename_history)

    def open_history(self):
        dialog = RenameHistoryDialog(
            self.rename_history,
            self,
        )
        dialog.setStyleSheet(
            self.styleSheet()
        )

        if (
            dialog.exec()
            != QDialog.DialogCode.Accepted
        ):
            return

        if dialog.selected_batch_id:
            self.undo_history_batch(
                dialog.selected_batch_id
            )

    def update_loaded_rows_after_undo(self, batch_items):
        for item in batch_items:
            if item.get("kind") != "video":
                continue

            old_path = item["old"]
            new_path = item["new"]

            for row in range(self.table.rowCount()):
                original_item = self.table.item(row, 0)
                if not original_item:
                    continue

                current_path = original_item.data(
                    Qt.ItemDataRole.UserRole
                )

                if (
                    current_path
                    and str(Path(current_path)).casefold()
                    == str(new_path).casefold()
                ):
                    original_item.setText(old_path.name)
                    original_item.setData(
                        Qt.ItemDataRole.UserRole,
                        str(old_path),
                    )
                    self.table.setItem(
                        row,
                        7,
                        QTableWidgetItem(
                            "✓ Undo Complete"
                        ),
                    )

        self.loaded_files = {
            self.table.item(row, 0).data(
                Qt.ItemDataRole.UserRole
            )
            for row in range(
                self.table.rowCount()
            )
            if self.table.item(row, 0)
        }

    def undo_history_batch(self, batch_id):
        batch = next(
            (
                entry
                for entry in self.rename_history
                if entry.get("id") == batch_id
            ),
            None,
        )

        if not batch or batch.get("status") != "renamed":
            QMessageBox.information(
                self,
                "History",
                "That rename batch is no longer active.",
            )
            return

        batch_items = [
            {
                "row": item.get("row"),
                "old": Path(item["old"]),
                "new": Path(item["new"]),
                "kind": item.get("kind", "video"),
                "created_directories": [
                    Path(path)
                    for path in item.get(
                        "created_directories",
                        [],
                    )
                ],
            }
            for item in batch.get("items", [])
        ]

        errors = []

        for item in batch_items:
            if not item["new"].exists():
                errors.append(
                    f"Renamed file is missing: {item['new']}"
                )

            if item["old"].exists():
                errors.append(
                    f"Original filename is already occupied: "
                    f"{item['old']}"
                )

        if errors:
            QMessageBox.critical(
                self,
                "Undo Blocked",
                "Rogue Renamer cannot safely undo this batch:\n\n"
                + "\n".join(errors[:12]),
            )
            return

        answer = QMessageBox.question(
            self,
            "Undo Rename Batch",
            f"Restore the original names for "
            f"{len(batch_items)} file(s)?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if answer != QMessageBox.StandardButton.Yes:
            return

        undone = []

        try:
            for item in reversed(batch_items):
                item["new"].rename(
                    item["old"]
                )

                for directory in item.get(
                    "created_directories",
                    [],
                ):
                    try:
                        directory.rmdir()
                    except OSError:
                        pass

                undone.append(item)

        except Exception as error:
            rollback_errors = []

            for item in reversed(undone):
                try:
                    if (
                        item["old"].exists()
                        and not item["new"].exists()
                    ):
                        item["old"].rename(
                            item["new"]
                        )
                except Exception as rollback_error:
                    rollback_errors.append(
                        str(rollback_error)
                    )

            message = f"Undo failed:\n\n{error}"

            if rollback_errors:
                message += (
                    "\n\nSome recovery operations also failed:\n"
                    + "\n".join(rollback_errors)
                )

            QMessageBox.critical(
                self,
                "Undo Failed",
                message,
            )
            return

        self.mark_batch_undone(batch_id)
        self.update_loaded_rows_after_undo(
            batch_items
        )

        latest = self.get_latest_active_batch()
        self.last_rename_batch = (
            self.get_latest_active_batch_items()
        )
        self.undo_button.setEnabled(
            bool(latest)
        )
        self.update_rename_state()

        QMessageBox.information(
            self,
            "Undo Complete",
            "The original filenames have been restored.",
        )


    def metadata_provider_changed(self, _index):
        provider_id = self.provider_combo.currentData()
        if not provider_id:
            return

        try:
            set_active_provider(provider_id)
        except TMDBError as error:
            QMessageBox.warning(self, "Metadata Provider", str(error))
            return

        provider_name = get_active_provider_name()
        self.status_label.setText(
            f"Metadata provider changed to {provider_name}. "
            "Run Search Metadata to refresh matches."
        )

    def open_library_audit(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Library to Audit",
        )
        if not folder:
            return

        # Open immediately; LibraryAuditDialog performs the scan and metadata
        # work on its background worker.
        dialog = LibraryAuditDialog(folder, [], self)
        dialog.exec()

    def apply_interface_mode(self):
        """Show the simple rename workflow or Rogue's complete toolset."""
        basic = self.current_mode == "basic"

        # Basic keeps only the controls needed for add -> match -> review -> rename.
        self.history_button.setVisible(not basic)
        self.audit_button.setVisible(not basic)
        self.undo_button.setVisible(not basic)

        self.mode_button.setText(
            "Switch to Advanced" if basic else "Switch to Basic"
        )
        self.setWindowTitle(
            "Rogue Renamer — Basic" if basic else "Rogue Renamer — Advanced"
        )

    def toggle_mode(self):
        self.current_mode = (
            "advanced" if self.current_mode == "basic" else "basic"
        )
        config = load_config()
        config.setdefault("ui", {})
        config["ui"]["preferred_mode"] = self.current_mode
        save_config(config)
        self.apply_interface_mode()

    def _basic_destination_path(self, original_path, proposed_name):
        """Basic mode never moves media; it only renames beside the source."""
        return Path(original_path).with_name(proposed_name)

    def open_settings(self):
        dialog = SettingsDialog(
            self
        )

        dialog.setStyleSheet(
            self.styleSheet()
        )

        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh_proposed_filenames()
            self.current_mode = (
                load_config().get("ui", {}).get("preferred_mode", self.current_mode)
                or self.current_mode
            )
            self.apply_interface_mode()

    def refresh_proposed_filenames(self):
        for row in range(self.table.rowCount()):
            match_item = self.table.item(row, 4)
            original_item = self.table.item(row, 0)

            if not match_item or not original_item:
                continue

            match = match_item.data(Qt.ItemDataRole.UserRole)
            if not match:
                continue

            filepath = original_item.data(Qt.ItemDataRole.UserRole)

            try:
                proposed = build_proposed_filename(filepath, match)
            except ValueError:
                continue

            proposed_item = QTableWidgetItem(proposed)
            proposed_item.setToolTip(proposed)
            self.table.setItem(
                row,
                6,
                proposed_item,
            )

        self.update_rename_state()

    def select_folder(self):
        folder = (
            QFileDialog
            .getExistingDirectory(
                self,
                "Select Media Folder",
            )
        )

        if folder:
            self.scan_folder(
                folder
            )

    def select_files(self):
        files, _ = (
            QFileDialog
            .getOpenFileNames(
                self,
                "Select Media Files",
            )
        )

        for filepath in files:
            self.add_file(
                filepath
            )

    def scan_folder(self, folder):
        root = Path(folder)
        if not root.exists() or not root.is_dir():
            return 0

        iterator = (
            root.rglob("*")
            if self.scan_subfolders_checkbox.isChecked()
            else root.glob("*")
        )

        before = len(self.loaded_files)

        for path in iterator:
            if (
                path.is_file()
                and path.suffix.lower() in MEDIA_EXTENSIONS
                and not is_extra_video(path)
            ):
                self.add_file(str(path))

        return len(self.loaded_files) - before


    def add_file(self, filepath):
        filepath = os.path.abspath(
            filepath
        )

        if filepath in self.loaded_files:
            return

        path = Path(filepath)

        if (
            path.suffix.lower()
            not in MEDIA_EXTENSIONS
        ):
            return

        if is_extra_video(path):
            return

        parsed = parse_media_path(
            filepath
        )

        self.loaded_files.add(
            filepath
        )

        row = self.table.rowCount()

        self.table.insertRow(
            row
        )

        display_name = path.name
        parent_name = path.parent.name
        if parent_name:
            display_name = f"{parent_name} / {path.name}"

        original = QTableWidgetItem(
            display_name
        )
        original.setToolTip(filepath)

        original.setData(
            Qt.ItemDataRole.UserRole,
            filepath,
        )

        self.table.setItem(
            row,
            0,
            original,
        )

        self.table.setItem(
            row,
            1,
            QTableWidgetItem(
                parsed["type"]
            ),
        )

        parsed_title_item = QTableWidgetItem(
            parsed["title"]
        )

        folder_hints = []
        if parsed.get("folder_title"):
            folder_hints.append(
                f"Folder title: {parsed['folder_title']}"
            )
        if parsed.get("folder_year"):
            folder_hints.append(
                f"Folder year: {parsed['folder_year']}"
            )
        if parsed.get("folder_season") is not None:
            folder_hints.append(
                f"Folder season: {parsed['folder_season']}"
            )

        if folder_hints:
            parsed_title_item.setToolTip(
                "\n".join(folder_hints)
            )

        self.table.setItem(
            row,
            2,
            parsed_title_item,
        )

        if parsed["type"] == "TV":
            se = format_episode_code(parsed)
        else:
            se = ""

        self.table.setItem(
            row,
            3,
            QTableWidgetItem(se),
        )

        self.table.setItem(
            row,
            4,
            QTableWidgetItem(""),
        )

        self.table.setItem(
            row,
            5,
            QTableWidgetItem(""),
        )

        self.table.setItem(
            row,
            6,
            QTableWidgetItem(""),
        )

        self.table.setItem(
            row,
            7,
            QTableWidgetItem(
                "Ready to Search"
                if parsed["type"]
                != "Unknown"
                else "Needs Review"
            ),
        )

        self.update_status()

    def get_automation_settings(self):
        config = load_config()
        automation = config.get("automation", {})
        return {
            "auto_accept_threshold": int(
                automation.get("auto_accept_threshold", 90)
            ),
            "review_all_matches": bool(
                automation.get("review_all_matches", False)
            ),
        }

    def row_needs_review(self, row):
        status_item = self.table.item(row, 7)
        status = status_item.text() if status_item else ""
        return (
            "Review" in status
            or "Low Match" in status
            or "Needs Review" in status
        )

    def review_next_match(self):
        for row in range(self.table.rowCount()):
            if self.row_needs_review(row):
                match_item = self.table.item(row, 4)
                if (
                    match_item
                    and match_item.data(Qt.ItemDataRole.UserRole)
                ):
                    self.table.selectRow(row)
                    self.table.scrollToItem(match_item)
                    self.review_match(row, 4)
                    return

        QMessageBox.information(
            self,
            "Review Matches",
            "There are no queued metadata matches to review.",
        )

    def update_batch_summary(self):
        count = self.table.rowCount()

        if count == 0:
            self.status_label.setText("0 files loaded")
            self.review_button.setEnabled(False)
            return

        ready = 0
        review = 0
        failed = 0
        pending = 0

        for row in range(count):
            status_item = self.table.item(row, 7)
            status = status_item.text() if status_item else ""

            if (
                "No Match" in status
                or "Error:" in status
            ):
                failed += 1
            elif self.row_needs_review(row):
                review += 1
            elif (
                "Ready to Search" in status
                or "Searching" in status
            ):
                pending += 1
            elif (
                "High Match" in status
                or "Auto Accepted" in status
                or "Manually Confirmed" in status
                or "Renamed" in status
                or "Undo Complete" in status
            ):
                ready += 1
            else:
                pending += 1

        parts = [f"{count} file{'s' if count != 1 else ''}"]
        if ready:
            parts.append(f"{ready} Ready")
        if review:
            parts.append(f"{review} Need Review")
        if failed:
            parts.append(f"{failed} Failed")
        if pending:
            parts.append(f"{pending} Pending")

        self.status_label.setText(" • ".join(parts))
        self.review_button.setEnabled(review > 0)

    def search_metadata(self):
        automation = self.get_automation_settings()
        threshold = automation["auto_accept_threshold"]
        review_all = automation["review_all_matches"]

        self.search_button.setEnabled(
            False
        )

        QApplication.setOverrideCursor(
            Qt.CursorShape.WaitCursor
        )

        try:
            for row in range(
                self.table.rowCount()
            ):
                original_item = (
                    self.table.item(
                        row,
                        0,
                    )
                )

                filepath = (
                    original_item.data(
                        Qt.ItemDataRole.UserRole
                    )
                )

                parsed = parse_media_path(
                    filepath
                )

                if parsed["type"] == "Unknown":
                    self.table.setItem(
                        row,
                        7,
                        QTableWidgetItem(
                            "Needs Review"
                        ),
                    )
                    continue

                self.table.setItem(
                    row,
                    7,
                    QTableWidgetItem(
                        "Searching..."
                    ),
                )

                QApplication.processEvents()

                try:
                    match = match_media(
                        parsed
                    )

                except TMDBError as error:
                    self.table.setItem(
                        row,
                        7,
                        QTableWidgetItem(
                            f"Error: {error}"
                        ),
                    )
                    continue

                if not match:
                    self.table.setItem(
                        row,
                        7,
                        QTableWidgetItem(
                            "No Match"
                        ),
                    )
                    continue

                match_item = QTableWidgetItem(
                    match["title"]
                )

                match_item.setData(
                    Qt.ItemDataRole.UserRole,
                    match,
                )

                self.table.setItem(
                    row,
                    4,
                    match_item,
                )

                episode_title = (
                    match.get(
                        "episode_title"
                    )
                    or ""
                )

                self.table.setItem(
                    row,
                    5,
                    QTableWidgetItem(
                        episode_title
                    ),
                )

                try:
                    proposed = (
                        build_proposed_filename(
                            filepath,
                            match,
                        )
                    )
                except ValueError as error:
                    self.table.setItem(
                        row,
                        7,
                        QTableWidgetItem(f"Error: {error}"),
                    )
                    continue

                self.table.setItem(
                    row,
                    6,
                    QTableWidgetItem(
                        proposed
                    ),
                )

                confidence = match.get(
                    "confidence",
                    "Review",
                )

                score = match.get(
                    "score",
                    0,
                )

                auto_accept = (
                    not review_all
                    and confidence == "High"
                    and score >= threshold
                )

                if auto_accept:
                    status = (
                        f"✓ Auto Accepted ({score})"
                    )

                elif review_all:
                    status = (
                        f"⚠ Review — review-all enabled ({score})"
                    )

                elif confidence == "High":
                    status = (
                        f"⚠ Review — below {threshold} threshold ({score})"
                    )

                elif confidence == "Review":
                    status = (
                        f"⚠ Review — double-click match ({score})"
                    )

                else:
                    status = (
                        f"⚠ Low Match ({score})"
                    )

                self.table.setItem(
                    row,
                    7,
                    QTableWidgetItem(status),
                )

        finally:
            QApplication.restoreOverrideCursor()

            self.search_button.setEnabled(
                True
            )
            self.update_batch_summary()
            self.update_rename_state()


    def review_match(self, row, column):
        match_item = self.table.item(
            row,
            4,
        )

        if not match_item:
            return

        match = match_item.data(
            Qt.ItemDataRole.UserRole
        )

        if not match:
            return

        candidates = match.get(
            "candidates",
            [],
        )

        if not candidates:
            return

        original_item = self.table.item(
            row,
            0,
        )

        filepath = original_item.data(
            Qt.ItemDataRole.UserRole
        )

        parsed = parse_media_path(filepath)

        dialog = MatchSelectionDialog(
            parsed,
            candidates,
            self,
        )

        dialog.setStyleSheet(
            self.styleSheet()
        )

        if (
            dialog.exec()
            != QDialog.DialogCode.Accepted
        ):
            return

        selected = dialog.selected_match

        if not selected:
            return

        self.apply_manual_match(
            row,
            filepath,
            selected,
            candidates,
        )

    def apply_manual_match(
        self,
        row,
        filepath,
        match,
        candidates,
    ):
        stored_match = {
            **match,
            "candidates": candidates,
            "confidence": "Manual",
        }

        match_item = QTableWidgetItem(
            match["title"]
        )

        match_item.setData(
            Qt.ItemDataRole.UserRole,
            stored_match,
        )

        self.table.setItem(
            row,
            4,
            match_item,
        )

        episode_title = (
            match.get("episode_title")
            or ""
        )

        self.table.setItem(
            row,
            5,
            QTableWidgetItem(
                episode_title
            ),
        )

        try:
            proposed = build_proposed_filename(
                filepath,
                match,
            )
        except ValueError as error:
            QMessageBox.warning(
                self,
                "Invalid Naming Template",
                str(error),
            )
            return

        self.table.setItem(
            row,
            6,
            QTableWidgetItem(
                proposed
            ),
        )

        self.table.setItem(
            row,
            7,
            QTableWidgetItem(
                "✓ Manually Confirmed"
            ),
        )

        self.update_batch_summary()
        self.update_rename_state()

    def row_is_safe_to_rename(self, row):
        """Return True only for a row Rogue has already accepted/confirmed."""
        match_item = self.table.item(row, 4)
        proposed_item = self.table.item(row, 6)
        status_item = self.table.item(row, 7)

        if not match_item or not match_item.data(Qt.ItemDataRole.UserRole):
            return False

        if not proposed_item or not proposed_item.text().strip():
            return False

        status = status_item.text() if status_item else ""

        blocked_markers = (
            "Review",
            "Low Match",
            "No Match",
            "Needs Review",
            "Error:",
            "Searching",
            "Ready to Search",
        )
        if any(marker in status for marker in blocked_markers):
            return False

        return (
            "Auto Accepted" in status
            or "Manually Confirmed" in status
            or "High Match" in status
        )

    def update_rename_state(self):
        """Enable Rename whenever at least one loaded row is safe to rename."""
        has_safe_row = any(
            self.row_is_safe_to_rename(row)
            for row in range(self.table.rowCount())
        )
        self.rename_button.setEnabled(has_safe_row)


    def build_audit_repair_plan(self, fix_plan):
        """Build a validated video+companion plan for one approved audit repair."""
        errors = []
        plan = []
        destinations = set()

        source = Path(fix_plan.get("current", ""))
        destination = Path(fix_plan.get("suggested", ""))

        if not fix_plan.get("available"):
            return [], ["The audit finding does not have an approved fix plan."]

        if not source.exists() or not source.is_file():
            return [], [f"Source file is missing: {source}"]

        match = fix_plan.get("match") or {}
        if match.get("confidence") != "High":
            return [], ["Audit repairs require a High-confidence metadata match."]

        batch_items = [{
            "row": None,
            "source": source,
            "destination": destination,
            "kind": "video",
        }]

        for companion in find_companion_files(source):
            batch_items.append({
                "row": None,
                "source": companion,
                "destination": build_companion_destination(
                    destination,
                    companion,
                ),
                "kind": "companion",
            })

        for item in batch_items:
            item_source = item["source"]
            item_destination = item["destination"]
            source_key = str(item_source.absolute()).casefold()
            destination_key = str(item_destination.absolute()).casefold()

            if destination_key in destinations:
                errors.append(f"Duplicate destination: {item_destination}")
                continue
            destinations.add(destination_key)

            if not item_source.exists():
                errors.append(f"Source file is missing: {item_source}")
                continue

            if source_key == destination_key:
                continue

            if item_destination.exists():
                errors.append(f"Destination already exists: {item_destination}")
                continue

            plan.append(item)

        return plan, errors

    def execute_validated_plan(self, plan):
        """Execute a prevalidated plan with rollback and History/Undo support."""
        completed = []

        try:
            for item in plan:
                source = item["source"]
                destination = item["destination"]

                if not source.exists():
                    raise FileNotFoundError(
                        f"Source disappeared before repair: {source}"
                    )
                if destination.exists():
                    raise FileExistsError(
                        f"Destination appeared during repair: {destination}"
                    )

                created_directories = []
                parent = destination.parent
                missing = []
                cursor = parent
                while not cursor.exists():
                    missing.append(cursor)
                    cursor = cursor.parent

                parent.mkdir(parents=True, exist_ok=True)
                created_directories.extend(missing)
                source.rename(destination)

                completed.append({
                    "row": item.get("row"),
                    "old": source,
                    "new": destination,
                    "kind": item.get("kind", "video"),
                    "created_directories": created_directories,
                })

        except Exception as error:
            rollback_errors = []

            for item in reversed(completed):
                try:
                    if item["new"].exists() and not item["old"].exists():
                        item["new"].rename(item["old"])

                    for directory in item.get("created_directories", []):
                        try:
                            directory.rmdir()
                        except OSError:
                            pass
                except Exception as rollback_error:
                    rollback_errors.append(str(rollback_error))

            message = (
                f"Repair failed:\n\n{error}\n\n"
                "Any completed file operations were rolled back."
            )
            if rollback_errors:
                message += (
                    "\n\nSome rollback operations also failed:\n"
                    + "\n".join(rollback_errors)
                )

            QMessageBox.critical(self, "Audit Repair Failed", message)
            return False

        if not completed:
            return False

        self.save_completed_batch(completed)
        self.last_rename_batch = completed
        self.undo_button.setEnabled(True)
        return True

    def apply_audit_fix(self, fix_plan, finding):
        """Confirm and apply one High-confidence audit repair."""
        plan, errors = self.build_audit_repair_plan(fix_plan)

        if errors:
            QMessageBox.critical(
                self,
                "Audit Repair Blocked",
                "Rogue Renamer found problems and did not change anything:\n\n"
                + "\n".join(errors[:12])
                + (
                    f"\n\n...and {len(errors) - 12} more."
                    if len(errors) > 12
                    else ""
                ),
            )
            return False

        if not plan:
            QMessageBox.information(
                self,
                "Nothing to Repair",
                "The selected file already matches the suggested destination.",
            )
            return False

        confirm_dialog = RenameConfirmationDialog(plan, self)
        confirm_dialog.setWindowTitle("Confirm Audit Repair")
        confirm_dialog.setStyleSheet(self.styleSheet())

        if confirm_dialog.exec() != QDialog.DialogCode.Accepted:
            return False

        return self.execute_validated_plan(plan)


    def build_rename_plan(self):
        """Build a safe rename plan and isolate conflicting rows."""
        row_batches = []
        hard_errors = []

        for row in range(self.table.rowCount()):
            if not self.row_is_safe_to_rename(row):
                continue

            original_item = self.table.item(row, 0)
            proposed_item = self.table.item(row, 6)
            match_item = self.table.item(row, 4)

            if not original_item or not proposed_item or not match_item:
                hard_errors.append(
                    f"Row {row + 1}: missing rename information."
                )
                continue

            source = Path(
                original_item.data(Qt.ItemDataRole.UserRole)
            )
            proposed_name = proposed_item.text().strip()
            match = match_item.data(Qt.ItemDataRole.UserRole)

            if not proposed_name or not match:
                hard_errors.append(
                    f"{source.name}: missing proposed filename or match."
                )
                continue

            try:
                destination = build_destination_path(
                    source,
                    match,
                    proposed_name,
                )
            except ValueError as error:
                hard_errors.append(f"{source.name}: {error}")
                continue

            items = [{
                "row": row,
                "source": source,
                "destination": destination,
                "kind": "video",
            }]

            for companion in find_companion_files(source):
                items.append({
                    "row": row,
                    "source": companion,
                    "destination": build_companion_destination(
                        destination,
                        companion,
                    ),
                    "kind": "companion",
                })

            row_batches.append({
                "row": row,
                "video_source": source,
                "video_destination": destination,
                "items": items,
            })

        # Map every proposed destination to the rows that want it.
        destination_rows = {}
        for batch in row_batches:
            for item in batch["items"]:
                key = str(item["destination"].absolute()).casefold()
                destination_rows.setdefault(key, set()).add(batch["row"])

        conflicting_rows = set()
        for rows in destination_rows.values():
            if len(rows) > 1:
                conflicting_rows.update(rows)

        safe_plan = []
        skipped_conflicts = []

        for batch in row_batches:
            row = batch["row"]

            if row in conflicting_rows:
                skipped_conflicts.append({
                    "row": row,
                    "source": batch["video_source"],
                    "destination": batch["video_destination"],
                    "reason": "duplicate destination",
                })
                continue

            batch_has_error = False
            batch_plan = []

            for item in batch["items"]:
                source = item["source"]
                destination = item["destination"]

                source_key = str(source.absolute()).casefold()
                destination_key = str(destination.absolute()).casefold()

                if not source.exists():
                    hard_errors.append(
                        f"Source file is missing: {source}"
                    )
                    batch_has_error = True
                    break

                # Already at its intended location: nothing to do for this item.
                if source_key == destination_key:
                    continue

                if destination.exists():
                    hard_errors.append(
                        f"Destination already exists: {destination}"
                    )
                    batch_has_error = True
                    break

                batch_plan.append(item)

            if not batch_has_error:
                safe_plan.extend(batch_plan)

        return safe_plan, hard_errors, skipped_conflicts

    def rename_files(self):
        plan, errors, skipped_conflicts = self.build_rename_plan()

        # Missing sources / existing destination files remain hard blockers.
        # Duplicate destinations within THIS batch are different: Rogue can
        # safely omit every conflicting row and continue with unrelated files.
        if errors:
            QMessageBox.critical(
                self,
                "Rename Blocked",
                "Rogue Renamer found problems and did not "
                "rename anything:\n\n"
                + "\n".join(errors[:12])
                + (
                    f"\n\n...and {len(errors) - 12} more."
                    if len(errors) > 12
                    else ""
                ),
            )
            return

        if skipped_conflicts and plan:
            unique_destinations = []
            seen = set()
            for conflict in skipped_conflicts:
                key = str(conflict["destination"]).casefold()
                if key not in seen:
                    seen.add(key)
                    unique_destinations.append(conflict["destination"])

            safe_rows = len({
                item["row"]
                for item in plan
                if item.get("kind") == "video"
            })

            conflict_lines = "\n".join(
                f"• {destination}"
                for destination in unique_destinations[:6]
            )
            if len(unique_destinations) > 6:
                conflict_lines += (
                    f"\n• ...and {len(unique_destinations) - 6} more"
                )

            message = (
                f"Rogue found {len(skipped_conflicts)} accepted file(s) "
                "that conflict because they would use the same destination.\n\n"
                "Rogue will NOT choose between or delete duplicate media. "
                "Every conflicting copy will be left untouched.\n\n"
                f"Conflicting destination(s):\n{conflict_lines}\n\n"
                f"{safe_rows} other video file(s) are safe to rename.\n\n"
                "Continue with only the safe files?"
            )

            choice = QMessageBox.question(
                self,
                "Duplicate Destinations",
                message,
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if choice != QMessageBox.StandardButton.Yes:
                return

        elif skipped_conflicts and not plan:
            QMessageBox.warning(
                self,
                "Duplicates Need Review",
                "All accepted files in this batch conflict with another "
                "file that would use the same destination.\n\n"
                "Rogue did not rename or delete anything. Review the "
                "duplicates and choose which copy you want to keep.",
            )
            return

        if not plan:
            QMessageBox.information(
                self,
                "Nothing to Rename",
                "All files already have their proposed names.",
            )
            return

        confirm_dialog = RenameConfirmationDialog(
            plan,
            self,
        )
        confirm_dialog.setStyleSheet(
            self.styleSheet()
        )

        if (
            confirm_dialog.exec()
            != QDialog.DialogCode.Accepted
        ):
            return

        completed = []

        try:
            for item in plan:
                source = item["source"]
                destination = item["destination"]

                # Re-check immediately before each rename.
                if destination.exists():
                    raise FileExistsError(
                        f"Destination appeared during rename: "
                        f"{destination}"
                    )

                created_directories = []
                parent = destination.parent

                missing = []
                cursor = parent
                while not cursor.exists():
                    missing.append(cursor)
                    cursor = cursor.parent

                parent.mkdir(parents=True, exist_ok=True)
                created_directories.extend(missing)

                source.rename(destination)

                completed.append(
                    {
                        "row": item["row"],
                        "old": source,
                        "new": destination,
                        "kind": item.get("kind", "video"),
                        "created_directories": created_directories,
                    }
                )

        except Exception as error:
            # Roll back anything already renamed in this batch.
            rollback_errors = []

            for item in reversed(completed):
                try:
                    if (
                        item["new"].exists()
                        and not item["old"].exists()
                    ):
                        item["new"].rename(
                            item["old"]
                        )

                    for directory in item.get(
                        "created_directories",
                        [],
                    ):
                        try:
                            directory.rmdir()
                        except OSError:
                            pass
                except Exception as rollback_error:
                    rollback_errors.append(
                        str(rollback_error)
                    )

            message = (
                f"Rename failed:\n\n{error}\n\n"
                "Any completed renames were rolled back."
            )

            if rollback_errors:
                message += (
                    "\n\nSome rollback operations also failed:\n"
                    + "\n".join(rollback_errors)
                )

            QMessageBox.critical(
                self,
                "Rename Failed",
                message,
            )
            return

        self.save_completed_batch(completed)
        self.last_rename_batch = completed
        self.undo_button.setEnabled(
            bool(completed)
        )

        # Update table rows for videos. Companion files are managed
        # alongside their video but do not occupy separate table rows.
        for item in completed:
            if item.get("kind") != "video":
                continue

            row = item["row"]
            new_path = item["new"]

            original_item = self.table.item(
                row,
                0,
            )
            original_item.setText(
                new_path.name
            )
            original_item.setData(
                Qt.ItemDataRole.UserRole,
                str(new_path),
            )

            companion_count = sum(
                1
                for entry in completed
                if entry.get("row") == row
                and entry.get("kind") == "companion"
            )

            status = "✓ Renamed"
            if companion_count:
                status += (
                    f" + {companion_count} companion"
                    f"{'s' if companion_count != 1 else ''}"
                )

            self.table.setItem(
                row,
                7,
                QTableWidgetItem(status),
            )

        self.loaded_files = {
            self.table.item(row, 0).data(
                Qt.ItemDataRole.UserRole
            )
            for row in range(
                self.table.rowCount()
            )
        }

        self.update_rename_state()

        QMessageBox.information(
            self,
            "Rename Complete",
            f"Successfully renamed "
            f"{len(completed)} file(s).\n\n"
            "You can use Undo Last Rename to restore "
            "the original filenames.",
        )

    def undo_last_rename(self):
        batch = self.get_latest_active_batch()

        if not batch:
            self.last_rename_batch = []
            self.undo_button.setEnabled(False)
            return

        self.undo_history_batch(
            batch.get("id")
        )


    def clear_files(self):
        self.table.setRowCount(
            0
        )

        self.loaded_files.clear()

        self.update_status()
        self.update_rename_state()

    def update_status(self):
        count = self.table.rowCount()
        self.update_batch_summary()
        self.search_button.setEnabled(count > 0)


    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        for url in (
            event.mimeData().urls()
        ):
            path = url.toLocalFile()

            if os.path.isdir(path):
                self.scan_folder(path)

            elif os.path.isfile(path):
                self.add_file(path)

        event.acceptProposedAction()


def main():
    app = QApplication(
        sys.argv
    )

    app.setApplicationName(
        "Rogue Renamer"
    )

    config = load_config()

    if not config.get("ui", {}).get("setup_complete", False):
        setup = FirstRunSetupDialog()
        if setup.exec() != QDialog.DialogCode.Accepted:
            return

    window = RogueRenamer()

    window.show()

    sys.exit(
        app.exec()
    )

if __name__ == "__main__":
    main()
