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
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
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


class MatchSelectionDialog(QDialog):
    def __init__(self, parsed, candidates, parent=None):
        super().__init__(parent)

        self.parsed = parsed
        self.candidates = candidates
        self.selected_match = None

        self.setWindowTitle(
            f"Choose Match — {parsed['title']}"
        )
        self.resize(760, 520)

        layout = QVBoxLayout(self)

        heading = QLabel(
            f"Choose the correct match for “{parsed['title']}”"
        )
        heading.setStyleSheet(
            "font-size: 20px; font-weight: bold;"
        )
        layout.addWidget(heading)

        explanation = QLabel(
            "Rogue Renamer found possible metadata matches. "
            "Select the correct title below."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet(
            """
            QListWidget {
                background: #1d2024;
                color: #eeeeee;
                border: 1px solid #444;
                padding: 4px;
            }
            QListWidget::item {
                color: #eeeeee;
                background: #1d2024;
                padding: 9px 7px;
                border-bottom: 1px solid #30343a;
            }
            QListWidget::item:selected {
                color: #ffffff;
                background: #3b4654;
            }
            QListWidget::item:hover {
                background: #2b3036;
            }
            """
        )

        for candidate in candidates:
            year = candidate.get("year") or "Unknown year"
            score = candidate.get("score", 0)
            title = candidate.get("title", "Unknown")

            if candidate["type"] == "TV":
                episode_title = candidate.get("episode_title")

                if episode_title:
                    detail = (
                        f"{title} ({year})   •   "
                        f"S{candidate['season']:02d}"
                        f"E{candidate['episode']:02d} exists"
                        f"   •   Match score {score}"
                    )
                else:
                    detail = (
                        f"{title} ({year})   •   "
                        f"Requested episode not found"
                        f"   •   Match score {score}"
                    )
            else:
                detail = (
                    f"{title} ({year})"
                    f"   •   Match score {score}"
                )

            item = QListWidgetItem(detail)
            item.setData(
                Qt.ItemDataRole.UserRole,
                candidate,
            )
            self.list_widget.addItem(item)

        layout.addWidget(self.list_widget)

        self.overview = QTextEdit()
        self.overview.setReadOnly(True)
        self.overview.setMaximumHeight(140)
        self.overview.setStyleSheet(
            """
            QTextEdit {
                background: #1d2024;
                color: #dddddd;
                border: 1px solid #444;
                padding: 7px;
                selection-background-color: #3b4654;
                selection-color: #ffffff;
            }
            """
        )
        layout.addWidget(self.overview)

        self.list_widget.currentItemChanged.connect(
            self.update_overview
        )
        self.list_widget.itemDoubleClicked.connect(
            lambda _item: self.use_selected()
        )

        buttons = QHBoxLayout()

        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)

        use_button = QPushButton("Use Selected Match")
        use_button.clicked.connect(self.use_selected)

        buttons.addStretch()
        buttons.addWidget(cancel_button)
        buttons.addWidget(use_button)

        layout.addLayout(buttons)

        if self.list_widget.count():
            self.list_widget.setCurrentRow(0)

    def update_overview(self, current, previous):
        if not current:
            self.overview.clear()
            return

        candidate = current.data(
            Qt.ItemDataRole.UserRole
        )

        overview = candidate.get(
            "overview",
            "",
        )

        if not overview:
            overview = "No description available."

        self.overview.setPlainText(overview)

    def use_selected(self):
        item = self.list_widget.currentItem()

        if not item:
            QMessageBox.warning(
                self,
                "Choose Match",
                "Select a match first.",
            )
            return

        candidate = item.data(
            Qt.ItemDataRole.UserRole
        )

        if (
            candidate["type"] == "TV"
            and not candidate.get("episode_title")
        ):
            QMessageBox.warning(
                self,
                "Episode Not Found",
                "The requested season and episode "
                "does not exist for this show.",
            )
            return

        self.selected_match = candidate
        self.accept()


class RogueRenamer(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle(
            "Rogue Renamer"
        )

        self.resize(1500, 760)

        self.loaded_files = set()
        self.last_rename_batch = []

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

        self.rename_button = QPushButton(
            "Rename Files"
        )

        self.rename_button.setEnabled(
            False
        )
        self.rename_button.clicked.connect(
            self.rename_files
        )

        self.undo_button = QPushButton(
            "Undo Last Rename"
        )
        self.undo_button.setEnabled(False)
        self.undo_button.clicked.connect(
            self.undo_last_rename
        )

        footer.addWidget(
            self.status_label
        )

        footer.addStretch()

        footer.addWidget(
            self.search_button
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

                confidence = match.get(
                    "confidence",
                    "Review",
                )

                score = match.get(
                    "score",
                    0,
                )

                if confidence == "High":
                    status = (
                        f"✓ High Match ({score})"
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

        parsed = parse_filename(filepath)

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

        proposed = build_proposed_filename(
            filepath,
            match,
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
                "✓ Manually Confirmed"
            ),
        )

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
        """Validate all destinations before changing any files."""
        plan = []
        destinations = set()
        errors = []

        for row in range(self.table.rowCount()):
            original_item = self.table.item(row, 0)
            proposed_item = self.table.item(row, 6)

            if not original_item or not proposed_item:
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

            if not proposed_name:
                errors.append(
                    f"{source.name}: no proposed filename."
                )
                continue

            destination = source.with_name(
                proposed_name
            )

            source_key = str(source).casefold()
            destination_key = str(destination).casefold()

            if destination_key in destinations:
                errors.append(
                    f"Duplicate destination: {destination.name}"
                )
                continue

            destinations.add(destination_key)

            if not source.exists():
                errors.append(
                    f"Source file is missing: {source}"
                )
                continue

            # Same filename/path means there is nothing to do.
            if source_key == destination_key:
                continue

            # Never overwrite another existing file.
            if destination.exists():
                errors.append(
                    f"Destination already exists: {destination}"
                )
                continue

            plan.append(
                {
                    "row": row,
                    "source": source,
                    "destination": destination,
                }
            )

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

        preview_lines = []

        for item in plan[:15]:
            preview_lines.append(
                f"{item['source'].name}\n"
                f"  → {item['destination'].name}"
            )

        if len(plan) > 15:
            preview_lines.append(
                f"...and {len(plan) - 15} more file(s)."
            )

        answer = QMessageBox.question(
            self,
            "Confirm Rename",
            f"Rename {len(plan)} file(s)?\n\n"
            + "\n\n".join(preview_lines)
            + "\n\nNo existing files will be overwritten.",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if answer != QMessageBox.StandardButton.Yes:
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

                source.rename(destination)

                completed.append(
                    {
                        "row": item["row"],
                        "old": source,
                        "new": destination,
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

        self.last_rename_batch = completed
        self.undo_button.setEnabled(
            bool(completed)
        )

        # Update each row to the new on-disk path.
        for item in completed:
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

            self.table.setItem(
                row,
                7,
                QTableWidgetItem(
                    "✓ Renamed"
                ),
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
        if not self.last_rename_batch:
            return

        # Validate the entire undo first.
        errors = []

        for item in self.last_rename_batch:
            old_path = item["old"]
            new_path = item["new"]

            if not new_path.exists():
                errors.append(
                    f"Renamed file is missing: {new_path}"
                )

            if old_path.exists():
                errors.append(
                    f"Original filename is already occupied: "
                    f"{old_path}"
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
            "Undo Last Rename",
            f"Restore the original names for "
            f"{len(self.last_rename_batch)} file(s)?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if answer != QMessageBox.StandardButton.Yes:
            return

        undone = []

        try:
            for item in reversed(
                self.last_rename_batch
            ):
                item["new"].rename(
                    item["old"]
                )
                undone.append(item)

        except Exception as error:
            # Best effort: put already-undone files back to
            # their renamed state so the batch stays consistent.
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

            message = (
                f"Undo failed:\n\n{error}"
            )

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

        for item in self.last_rename_batch:
            row = item["row"]
            old_path = item["old"]

            original_item = self.table.item(
                row,
                0,
            )
            original_item.setText(
                old_path.name
            )
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
        }

        self.last_rename_batch = []
        self.undo_button.setEnabled(False)
        self.update_rename_state()

        QMessageBox.information(
            self,
            "Undo Complete",
            "The original filenames have been restored.",
        )

    def clear_files(self):
        self.table.setRowCount(
            0
        )

        self.loaded_files.clear()

        self.update_status()
        self.update_rename_state()

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