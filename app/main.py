import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QByteArray
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
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
)

import requests

from app.metadata.tmdb import (
    TMDBError,
    match_media,
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

        layout = QVBoxLayout(self)

        title = QLabel(
            "TMDB Metadata Provider"
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

            if candidate["type"] == "TV":
                state = (
                    format_episode_code(candidate)
                    if candidate.get("episode_title")
                    else "episode(s) missing"
                )
                detail = (
                    f"{title} ({year}){country_text}\n"
                    f"TMDB #{candidate.get('id', '?')}   •   {state}   •   Score {score}"
                )
            else:
                detail = (
                    f"{title} ({year}){country_text}\n"
                    f"TMDB #{candidate.get('id', '?')}   •   Score {score}"
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
        if not poster_path:
            self.poster.setPixmap(QPixmap())
            self.poster.setText("No poster available")
            return

        pixmap = self.poster_cache.get(poster_path)
        if pixmap is None:
            self.poster.setPixmap(QPixmap())
            self.poster.setText("Loading poster…")
            QApplication.processEvents()
            try:
                response = requests.get(
                    f"https://image.tmdb.org/t/p/w342{poster_path}",
                    timeout=8,
                )
                response.raise_for_status()
                pixmap = QPixmap()
                if not pixmap.loadFromData(QByteArray(response.content)):
                    pixmap = None
                if pixmap is not None:
                    self.poster_cache[poster_path] = pixmap
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
        tmdb_id = candidate.get("id", "?")
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
            f"<b>TMDB ID:</b> {tmdb_id}<br>"
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

        history_button = QPushButton(
            "History"
        )
        history_button.clicked.connect(
            self.open_history
        )

        header.addLayout(titles)
        header.addStretch()
        header.addWidget(
            history_button
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
                "TMDB Match",
                "Episode Title",
                "Proposed Filename",
                "Status",
            ]
        )

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

    def open_settings(self):
        dialog = SettingsDialog(
            self
        )

        dialog.setStyleSheet(
            self.styleSheet()
        )

        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh_proposed_filenames()

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

    def update_rename_state(self):
        """Enable Rename only when every loaded row is safe to rename."""
        count = self.table.rowCount()

        if count == 0:
            self.rename_button.setEnabled(False)
            return

        for row in range(count):
            match_item = self.table.item(row, 4)
            proposed_item = self.table.item(row, 6)
            status_item = self.table.item(row, 7)

            if not match_item or not match_item.data(
                Qt.ItemDataRole.UserRole
            ):
                self.rename_button.setEnabled(False)
                return

            if not proposed_item or not proposed_item.text().strip():
                self.rename_button.setEnabled(False)
                return

            status = (
                status_item.text()
                if status_item
                else ""
            )

            if (
                "Review" in status
                or "Low Match" in status
                or "No Match" in status
                or "Needs Review" in status
                or "Error:" in status
                or "Searching" in status
                or "Ready to Search" in status
            ):
                self.rename_button.setEnabled(False)
                return

        self.rename_button.setEnabled(True)

    def build_rename_plan(self):
        """Validate video and companion destinations before changing files."""
        plan = []
        destinations = set()
        errors = []

        for row in range(self.table.rowCount()):
            original_item = self.table.item(row, 0)
            proposed_item = self.table.item(row, 6)
            match_item = self.table.item(row, 4)

            if not original_item or not proposed_item or not match_item:
                errors.append(
                    f"Row {row + 1}: missing rename information."
                )
                continue

            source = Path(
                original_item.data(
                    Qt.ItemDataRole.UserRole
                )
            )
            proposed_name = proposed_item.text().strip()
            match = match_item.data(
                Qt.ItemDataRole.UserRole
            )

            if not proposed_name or not match:
                errors.append(
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
                errors.append(
                    f"{source.name}: {error}"
                )
                continue

            batch_items = [
                {
                    "row": row,
                    "source": source,
                    "destination": destination,
                    "kind": "video",
                }
            ]

            for companion in find_companion_files(source):
                companion_destination = build_companion_destination(
                    destination,
                    companion,
                )
                batch_items.append(
                    {
                        "row": row,
                        "source": companion,
                        "destination": companion_destination,
                        "kind": "companion",
                    }
                )

            for item in batch_items:
                item_source = item["source"]
                item_destination = item["destination"]

                source_key = str(
                    item_source.absolute()
                ).casefold()
                destination_key = str(
                    item_destination.absolute()
                ).casefold()

                if destination_key in destinations:
                    errors.append(
                        f"Duplicate destination: {item_destination}"
                    )
                    continue

                destinations.add(destination_key)

                if not item_source.exists():
                    errors.append(
                        f"Source file is missing: {item_source}"
                    )
                    continue

                if source_key == destination_key:
                    continue

                if item_destination.exists():
                    errors.append(
                        f"Destination already exists: {item_destination}"
                    )
                    continue

                plan.append(item)

        return plan, errors

    def rename_files(self):
        plan, errors = self.build_rename_plan()

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

    window = RogueRenamer()

    window.show()

    sys.exit(
        app.exec()
    )