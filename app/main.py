import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import requests

from app.metadata.tmdb import (
    TMDBError,
    match_media,
)
from app.parser import (
    MEDIA_EXTENSIONS,
    parse_filename,
)
from app.settings import (
    load_config,
    save_config,
)


def safe_filename(text):
    illegal = '<>:"/\\|?*'

    for character in illegal:
        text = text.replace(character, "")

    return text.strip()


def build_proposed_filename(
    original_path,
    match,
):
    path = Path(original_path)
    extension = path.suffix

    if match["type"] == "Movie":
        title = match["title"]
        year = match.get("year")

        if year:
            filename = f"{title} ({year})"
        else:
            filename = title

    else:
        season = match["season"]
        episode = match["episode"]

        episode_title = match.get(
            "episode_title",
            "",
        )

        filename = (
            f"{match['title']} - "
            f"S{season:02d}E{episode:02d}"
        )

        if episode_title:
            filename += f" - {episode_title}"

    return safe_filename(filename) + extension


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowTitle(
            "Rogue Renamer Settings"
        )

        self.resize(600, 260)

        self.config = load_config()

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

        save_config(self.config)

        self.accept()


class RogueRenamer(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle(
            "Rogue Renamer"
        )

        self.resize(1500, 760)

        self.loaded_files = set()

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

        header.addLayout(titles)
        header.addStretch()
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

        self.table.setColumnWidth(
            0,
            300,
        )

        self.table.setColumnWidth(
            1,
            75,
        )

        self.table.setColumnWidth(
            2,
            190,
        )

        self.table.setColumnWidth(
            3,
            80,
        )

        self.table.setColumnWidth(
            4,
            190,
        )

        self.table.setColumnWidth(
            5,
            200,
        )

        self.table.setColumnWidth(
            6,
            360,
        )

        self.table.horizontalHeader().setStretchLastSection(
            True
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

        self.rename_button = QPushButton(
            "Rename Files"
        )

        # IMPORTANT:
        # Renaming remains disabled.
        self.rename_button.setEnabled(
            False
        )

        footer.addWidget(
            self.status_label
        )

        footer.addStretch()

        footer.addWidget(
            self.search_button
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

    def open_settings(self):
        dialog = SettingsDialog(
            self
        )

        dialog.setStyleSheet(
            self.styleSheet()
        )

        dialog.exec()

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
        for path in Path(
            folder
        ).rglob("*"):

            if (
                path.is_file()
                and
                path.suffix.lower()
                in MEDIA_EXTENSIONS
            ):
                self.add_file(
                    str(path)
                )

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

        parsed = parse_filename(
            filepath
        )

        self.loaded_files.add(
            filepath
        )

        row = self.table.rowCount()

        self.table.insertRow(
            row
        )

        original = QTableWidgetItem(
            path.name
        )

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

        self.table.setItem(
            row,
            2,
            QTableWidgetItem(
                parsed["title"]
            ),
        )

        if parsed["type"] == "TV":
            se = (
                f"S{parsed['season']:02d}"
                f"E{parsed['episode']:02d}"
            )
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

    def search_metadata(self):
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

                parsed = parse_filename(
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

                self.table.setItem(
                    row,
                    4,
                    QTableWidgetItem(
                        match["title"]
                    ),
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

                proposed = (
                    build_proposed_filename(
                        filepath,
                        match,
                    )
                )

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
                        "Matched"
                    ),
                )

        finally:
            QApplication.restoreOverrideCursor()

            self.search_button.setEnabled(
                True
            )

    def clear_files(self):
        self.table.setRowCount(
            0
        )

        self.loaded_files.clear()

        self.update_status()

    def update_status(self):
        count = (
            self.table.rowCount()
        )

        self.status_label.setText(
            f"{count} file"
            f"{'s' if count != 1 else ''}"
            " loaded"
        )

        self.search_button.setEnabled(
            count > 0
        )

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