#!/usr/bin/env python3
"""
fm_m1.py — Dual-Pane Dateimanager, Meilenstein 1

Umfang M1 (bewusst NICHT mehr):
  - Zwei Panes nebeneinander, unabhängig navigierbar
  - Breadcrumb-Pfadleiste mit Klick-Navigation
  - Sortierung, Ordner-zuerst
  - Öffnen von Dateien via xdg-open (System-Standardprogramm)
  - Tab wechselt aktives Pane, F5 aktualisiert, Strg+L fokussiert Pfadleiste
  - Statuszeile: Anzahl Einträge + Auswahl

Ausdrücklich NICHT in M1: Copy/Move/Delete, Trash, Docker-Pane, Git-Status,
Thumbnails, Undo. Kommt in M2+.

Abhängigkeiten:
    pip install PySide6

Start:
    python3 fm_m1.py [Startpfad]
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QDir, QModelIndex, Qt
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFileSystemModel,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QSplitter,
    QStatusBar,
    QTreeView,
    QVBoxLayout,
    QWidget,
)


def open_with_default_app(path: Path) -> None:
    """Öffnet eine Datei/einen Ordner mit dem System-Standardprogramm."""
    try:
        subprocess.Popen(["xdg-open", str(path)])
    except FileNotFoundError:
        # Fallback falls xdg-open mal fehlt (sollte auf CachyOS/KDE nicht passieren)
        subprocess.Popen(["kde-open5", str(path)])


class BreadcrumbBar(QWidget):
    """Klickbare Pfadleiste. Klick auf ein Segment springt dorthin.
    Strg+L (vom Pane aus getriggert) schaltet auf freien Text-Edit um.
    """

    def __init__(self, on_navigate, parent=None):
        super().__init__(parent)
        self._on_navigate = on_navigate
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(4, 2, 4, 2)
        self._layout.setSpacing(2)

        self._edit = QLineEdit(self)
        self._edit.hide()
        self._edit.returnPressed.connect(self._commit_edit)
        self._layout.addWidget(self._edit)

        self._crumb_container = QWidget(self)
        self._crumb_layout = QHBoxLayout(self._crumb_container)
        self._crumb_layout.setContentsMargins(0, 0, 0, 0)
        self._crumb_layout.setSpacing(0)
        self._layout.addWidget(self._crumb_container)
        self._layout.addStretch(1)

        self.set_path(Path.home())

    def set_path(self, path: Path) -> None:
        self._path = path
        self._edit.setText(str(path))
        self._rebuild_crumbs()

    def _rebuild_crumbs(self) -> None:
        while self._crumb_layout.count():
            item = self._crumb_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        parts = self._path.parts
        accum = Path(parts[0]) if parts else Path("/")
        for i, part in enumerate(parts):
            if i == 0:
                label_text = part if part != "/" else "/"
                accum = Path(part)
            else:
                accum = accum / part
                label_text = part

            btn = QLabel(f"<a href='#' style='text-decoration:none;'>{label_text}</a>")
            btn.setTextInteractionFlags(Qt.TextBrowserInteraction)
            btn.linkActivated.connect(lambda _, p=accum: self._on_navigate(p))
            self._crumb_layout.addWidget(btn)

            if i < len(parts) - 1:
                sep = QLabel(" / ")
                self._crumb_layout.addWidget(sep)

    def focus_edit(self) -> None:
        self._crumb_container.hide()
        self._edit.show()
        self._edit.setFocus()
        self._edit.selectAll()

    def _commit_edit(self) -> None:
        text = self._edit.text().strip()
        candidate = Path(text).expanduser()
        if candidate.is_dir():
            self._on_navigate(candidate)
        self._edit.hide()
        self._crumb_container.show()
        self.set_path(self._path)


class FilePane(QWidget):
    """Ein einzelnes Pane: Breadcrumb oben, QTreeView darunter."""

    def __init__(self, start_path: Path, status_callback, parent=None):
        super().__init__(parent)
        self.current_path = start_path
        self._status_callback = status_callback

        self.model = QFileSystemModel(self)
        self.model.setRootPath(QDir.rootPath())
        self.model.setFilter(
            QDir.AllEntries | QDir.NoDotAndDotDot | QDir.Hidden
        )

        self.view = QTreeView(self)
        self.view.setModel(self.model)
        self.view.setRootIndex(self.model.index(str(start_path)))
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(0, Qt.AscendingOrder)
        self.view.setAlternatingRowColors(True)
        self.view.setUniformRowHeights(True)
        self.view.setColumnWidth(0, 260)
        self.view.doubleClicked.connect(self._on_double_click)
        self.view.selectionModel().selectionChanged.connect(self._update_status)

        self.breadcrumb = BreadcrumbBar(self.navigate_to, self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.breadcrumb)
        layout.addWidget(self.view)

        QShortcut(QKeySequence("Backspace"), self.view, activated=self.go_up)
        QShortcut(QKeySequence("F5"), self.view, activated=self.refresh)

        self.navigate_to(start_path)

    def navigate_to(self, path: Path) -> None:
        if not path.is_dir():
            return
        self.current_path = path
        self.view.setRootIndex(self.model.index(str(path)))
        self.breadcrumb.set_path(path)
        self._update_status()

    def go_up(self) -> None:
        parent = self.current_path.parent
        if parent != self.current_path:
            self.navigate_to(parent)

    def refresh(self) -> None:
        # QFileSystemModel aktualisiert sich i.d.R. selbst per FS-Watcher;
        # explizites Re-Root erzwingt trotzdem einen sauberen Reload.
        self.navigate_to(self.current_path)

    def _on_double_click(self, index: QModelIndex) -> None:
        path = Path(self.model.filePath(index))
        if path.is_dir():
            self.navigate_to(path)
        else:
            open_with_default_app(path)

    def _update_status(self) -> None:
        total = self.model.rowCount(self.view.rootIndex())
        selected = len(self.view.selectionModel().selectedRows())
        self._status_callback(f"{selected} von {total} ausgewählt — {self.current_path}")

    def focus_breadcrumb_edit(self) -> None:
        self.breadcrumb.focus_edit()


class MainWindow(QMainWindow):
    def __init__(self, start_path: Path):
        super().__init__()
        self.setWindowTitle("fm — Dual-Pane (M1)")
        self.resize(1400, 800)
        self.setWindowIcon(QIcon.fromTheme("system-file-manager"))

        self.status = QStatusBar(self)
        self.setStatusBar(self.status)

        self.left = FilePane(start_path, self._status_from_left)
        self.right = FilePane(start_path, self._status_from_right)
        self.active_pane = self.left

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.addWidget(self.left)
        splitter.addWidget(self.right)
        splitter.setSizes([700, 700])
        self.setCentralWidget(splitter)

        self.left.view.installEventFilter(self)
        self.right.view.installEventFilter(self)
        self.left.view.clicked.connect(lambda _: self._set_active(self.left))
        self.right.view.clicked.connect(lambda _: self._set_active(self.right))

        QShortcut(QKeySequence("Tab"), self, activated=self._toggle_active_pane)
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self._focus_pathbar)

    def _set_active(self, pane: FilePane) -> None:
        self.active_pane = pane

    def _toggle_active_pane(self) -> None:
        self.active_pane = self.right if self.active_pane is self.left else self.left
        self.active_pane.view.setFocus()

    def _focus_pathbar(self) -> None:
        self.active_pane.focus_breadcrumb_edit()

    def _status_from_left(self, text: str) -> None:
        self.status.showMessage(f"Links: {text}")

    def _status_from_right(self, text: str) -> None:
        self.status.showMessage(f"Rechts: {text}")


def main() -> None:
    start = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path.home()
    app = QApplication(sys.argv)
    app.setApplicationName("fm")
    window = MainWindow(start)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()