#!/usr/bin/env python3
"""
fm.py — Dual-Pane Dateimanager mit Projekt-Scaffolder (F7)

Neu gegenüber der letzten Version:
  - Menüleiste (Datei/Bearbeiten/Ansicht/Gehe zu/Hilfe)
  - Drag & Drop zwischen den Panes und in Unterordner, Strg+Ziehen kopiert
  - Eingebettetes Terminal (F4): echte Shell über pty.fork() + ANSI-
    Rendering über pyte, kein QProcess-Gefrickel — Farben, vim/htop/less
    und interaktive Prompts funktionieren wie in einem echten Terminal

Voraussetzung für React/Vue/Angular: Node.js + npm/npx im PATH.

Abhängigkeiten:
    pip install -r requirements.txt

Start:
    python3 fm.py [Startpfad]
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
from datetime import datetime
from pathlib import Path

import pyte
from PySide6.QtCore import QDir, QEvent, QItemSelectionModel, QMimeDatabase, QModelIndex, QSize, QSocketNotifier, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QImageReader,
    QKeySequence,
    QPainter,
    QPixmap,
    QShortcut,
)
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileIconProvider,
    QFileSystemModel,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)


# --------------------------------------------------------------------------
# Toggle-Switch (moderner iOS-Style Switch statt Radio-Buttons)
# --------------------------------------------------------------------------

class ToggleSwitch(QCheckBox):
    """QCheckBox, per Stylesheet als runder Toggle-Switch dargestellt.
    checked=False -> linke Position, checked=True -> rechte Position.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(52, 28)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet("""
            QCheckBox {
                spacing: 0px;
            }
            QCheckBox::indicator {
                width: 52px;
                height: 28px;
                border-radius: 14px;
                background-color: #555;
            }
            QCheckBox::indicator:checked {
                background-color: #3daee9;
            }
        """)


class TargetDirSwitch(QWidget):
    """Label 'Hier' — Toggle — Label 'In ~/Dev', mit Fett-Markierung
    der jeweils aktiven Seite.
    """

    changed = Signal()

    def __init__(self, here_path: Path, dev_path: Path, parent=None):
        super().__init__(parent)
        self.here_path = here_path
        self.dev_path = dev_path

        self.label_here = QLabel("Hier")
        self.label_dev = QLabel("In ~/Dev")
        self.switch = ToggleSwitch(self)
        self.switch.toggled.connect(self._on_toggled)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label_here)
        layout.addWidget(self.switch)
        layout.addWidget(self.label_dev)
        layout.addStretch(1)

        self._on_toggled(False)

    def _on_toggled(self, checked: bool) -> None:
        active_style = "font-weight: bold;"
        inactive_style = "color: gray;"
        self.label_here.setStyleSheet(inactive_style if checked else active_style)
        self.label_dev.setStyleSheet(active_style if checked else inactive_style)
        self.changed.emit()

    def selected_path(self) -> Path:
        return self.dev_path if self.switch.isChecked() else self.here_path


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------

def open_with_default_app(path: Path) -> None:
    try:
        subprocess.Popen(["xdg-open", str(path)])
    except FileNotFoundError:
        subprocess.Popen(["kde-open5", str(path)])


# kind:
#   "manual-python" / "manual-node" / "manual-other" -> wir erzeugen die
#       Basisdateien selbst (schnell, kein Netzwerk nötig)
#   "npx" -> wir rufen den offiziellen CLI-Generator auf (braucht Node/npm,
#       Internet beim ersten Mal, dauert ein paar Sekunden)
STACK_PRESETS = {
    "Python / Flask": {
        "kind": "manual-python",
        "base_image": "python:3.12-slim",
        "run_cmd": "python app.py",
        "default_packages": ["flask"],
    },
    "Node": {
        "kind": "manual-node",
        "base_image": "node:22-slim",
        "run_cmd": "npm start",
        "default_packages": [],
    },
    "React (Vite)": {
        "kind": "npx",
        "base_image": "node:22-slim",
        "scaffold_cmd": ["npx", "--yes", "create-vite@latest", "{name}", "--", "--template", "react"],
        "run_cmd": "npm run dev -- --host 0.0.0.0",
        "default_packages": [],
    },
    "Vue (Vite)": {
        "kind": "npx",
        "base_image": "node:22-slim",
        "scaffold_cmd": ["npx", "--yes", "create-vite@latest", "{name}", "--", "--template", "vue"],
        "run_cmd": "npm run dev -- --host 0.0.0.0",
        "default_packages": [],
    },
    "Angular": {
        "kind": "npx",
        "base_image": "node:22-slim",
        "scaffold_cmd": ["npx", "--yes", "@angular/cli@latest", "new", "{name}", "--skip-git", "--defaults"],
        "run_cmd": "npm start -- --host 0.0.0.0",
        "default_packages": [],
    },
    ".NET Web API": {
        "kind": "dotnet",
        "base_image": "mcr.microsoft.com/dotnet/sdk:10.0",
        "scaffold_cmd": ["dotnet", "new", "webapi", "-o", "{name}", "--no-https"],
        "run_cmd": "dotnet run --urls http://0.0.0.0:5000",
        "default_packages": [],
    },
    ".NET Razor Pages (Website)": {
        "kind": "dotnet",
        "base_image": "mcr.microsoft.com/dotnet/sdk:10.0",
        "scaffold_cmd": ["dotnet", "new", "webapp", "-o", "{name}", "--no-https"],
        "run_cmd": "dotnet run --urls http://0.0.0.0:5000",
        "default_packages": [],
    },
    ".NET MVC (Website)": {
        "kind": "dotnet",
        "base_image": "mcr.microsoft.com/dotnet/sdk:10.0",
        "scaffold_cmd": ["dotnet", "new", "mvc", "-o", "{name}", "--no-https"],
        "run_cmd": "dotnet run --urls http://0.0.0.0:5000",
        "default_packages": [],
    },
    "Sonstiges": {
        "kind": "manual-other",
        "base_image": "debian:bookworm-slim",
        "run_cmd": "bash",
        "default_packages": [],
    },
}

# Nur Backend-Stacks bekommen die DB-Abfrage im Dialog angeboten —
# bei React/Vue/Angular (Frontend) und "Sonstiges" ist das irrelevant.
DB_RELEVANT_KINDS = {"manual-python", "manual-node", "dotnet"}

# SQLite bewusst NICHT hier drin — dateibasiert, braucht keinen eigenen
# Container/Service, nur ein Paket (siehe DB_PACKAGES).
DB_SERVICE_PRESETS = {
    "PostgreSQL": {
        "image": "postgres:16-alpine",
        "port": 5432,
        "env": {"POSTGRES_USER": "app", "POSTGRES_PASSWORD": "app", "POSTGRES_DB": "app"},
    },
    "MySQL": {
        "image": "mysql:8",
        "port": 3306,
        "env": {"MYSQL_ROOT_PASSWORD": "app", "MYSQL_DATABASE": "app"},
    },
    "SQL Server": {
        "image": "mcr.microsoft.com/mssql/server:2022-latest",
        "port": 1433,
        "env": {"ACCEPT_EULA": "Y", "MSSQL_SA_PASSWORD": "YourStrong!Passw0rd"},
    },
}

DB_CHOICES = ["Keine", "SQLite", "PostgreSQL", "MySQL", "SQL Server"]

DB_PACKAGES = {
    "manual-python": {
        "SQLite": ["flask-sqlalchemy"],
        "PostgreSQL": ["flask-sqlalchemy", "psycopg2-binary"],
        "MySQL": ["flask-sqlalchemy", "mysqlclient"],
        "SQL Server": ["flask-sqlalchemy", "pyodbc"],
    },
    "manual-node": {
        "SQLite": ["sqlite3"],
        "PostgreSQL": ["pg"],
        "MySQL": ["mysql2"],
        "SQL Server": ["mssql"],
    },
    "dotnet": {
        "SQLite": ["Microsoft.EntityFrameworkCore.Sqlite"],
        "PostgreSQL": ["Npgsql.EntityFrameworkCore.PostgreSQL"],
        "MySQL": ["Pomelo.EntityFrameworkCore.MySql"],
        "SQL Server": ["Microsoft.EntityFrameworkCore.SqlServer"],
    },
}


# --------------------------------------------------------------------------
# Projekt-Scaffolder-Dialog
# --------------------------------------------------------------------------

class NewProjectDialog(QDialog):
    def __init__(self, current_pane_dir: Path, parent=None):
        super().__init__(parent)
        self.current_pane_dir = current_pane_dir
        self.dev_dir = Path.home() / "Dev"
        self.setWindowTitle("Neues Projekt anlegen")
        self.resize(580, 660)

        self.name_edit = QLineEdit(self)
        self.name_edit.setPlaceholderText("z.B. mein-neues-projekt")

        self.target_switch = TargetDirSwitch(self.current_pane_dir, self.dev_dir, self)

        self.stack_combo = QComboBox(self)
        self.stack_combo.addItems(STACK_PRESETS.keys())
        self.stack_combo.currentTextChanged.connect(self._on_stack_changed)

        self.packages_edit = QPlainTextEdit(self)
        self.packages_edit.setPlaceholderText(
            "Zusätzliche Pakete, eins pro Zeile\n(bei React/Vue/Angular via npm install)"
        )
        self.packages_edit.setFixedHeight(70)

        self.db_label = QLabel("Datenbank:")
        self.db_combo = QComboBox(self)
        self.db_combo.addItems(DB_CHOICES)

        self.docker_checkbox = QCheckBox(
            "Projekt soll selbst in Docker laufen (nicht nur Dev-Container)", self
        )
        self.docker_checkbox.toggled.connect(self._on_docker_toggled)

        self.base_image_edit = QLineEdit(self)
        self.port_spin = QSpinBox(self)
        self.port_spin.setRange(0, 65535)

        self.env_table = QTableWidget(0, 2, self)
        self.env_table.setHorizontalHeaderLabels(["Variable", "Wert"])
        self.env_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        add_env_btn = QPushButton("+ Env-Var", self)
        add_env_btn.clicked.connect(self._add_env_row)
        remove_env_btn = QPushButton("– Zeile entfernen", self)
        remove_env_btn.clicked.connect(self._remove_env_row)

        env_btn_row = QHBoxLayout()
        env_btn_row.addWidget(add_env_btn)
        env_btn_row.addWidget(remove_env_btn)
        env_btn_row.addStretch(1)

        self.open_vscode_checkbox = QCheckBox("Nach dem Erstellen in VS Code öffnen", self)
        self.open_vscode_checkbox.setChecked(True)

        self.docker_fields_widget = QWidget(self)
        docker_form = QFormLayout(self.docker_fields_widget)
        docker_form.addRow("Base-Image:", self.base_image_edit)
        docker_form.addRow("Port:", self.port_spin)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self
        )
        self.buttons.button(QDialogButtonBox.Ok).setText("Erstellen")
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow("Projektname:", self.name_edit)
        form.addRow("Zielordner:", self.target_switch)
        form.addRow("Stack:", self.stack_combo)
        form.addRow("Pakete:", self.packages_edit)
        form.addRow(self.db_label, self.db_combo)
        layout.addLayout(form)
        layout.addWidget(self.docker_checkbox)
        layout.addWidget(self.docker_fields_widget)
        layout.addWidget(QLabel("Umgebungsvariablen:"))
        layout.addWidget(self.env_table)
        layout.addLayout(env_btn_row)
        layout.addWidget(self.open_vscode_checkbox)
        layout.addWidget(self.buttons)

        self._on_stack_changed(self.stack_combo.currentText())
        self._on_docker_toggled(False)

    def _on_stack_changed(self, stack: str) -> None:
        preset = STACK_PRESETS[stack]
        self.base_image_edit.setText(preset["base_image"])
        default_port = {
            "React (Vite)": 5173,
            "Vue (Vite)": 5173,
            "Angular": 4200,
            ".NET Web API": 5000,
            ".NET Razor Pages (Website)": 5000,
            ".NET MVC (Website)": 5000,
        }.get(stack, 8000)
        self.port_spin.setValue(default_port)
        self.packages_edit.setPlainText("\n".join(preset["default_packages"]))

        db_relevant = preset["kind"] in DB_RELEVANT_KINDS
        self.db_label.setVisible(db_relevant)
        self.db_combo.setVisible(db_relevant)
        if not db_relevant:
            self.db_combo.setCurrentText("Keine")

    def _on_docker_toggled(self, checked: bool) -> None:
        self.docker_fields_widget.setVisible(checked)
        self.env_table.setVisible(checked)

    def _add_env_row(self) -> None:
        row = self.env_table.rowCount()
        self.env_table.insertRow(row)
        self.env_table.setItem(row, 0, QTableWidgetItem(""))
        self.env_table.setItem(row, 1, QTableWidgetItem(""))

    def _remove_env_row(self) -> None:
        row = self.env_table.currentRow()
        if row >= 0:
            self.env_table.removeRow(row)

    def _collect_env_vars(self) -> dict[str, str]:
        env = {}
        for row in range(self.env_table.rowCount()):
            key_item = self.env_table.item(row, 0)
            val_item = self.env_table.item(row, 1)
            key = key_item.text().strip() if key_item else ""
            val = val_item.text().strip() if val_item else ""
            if key:
                env[key] = val
        return env

    def _on_accept(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Fehlt", "Bitte einen Projektnamen eingeben.")
            return

        target_dir = self.target_switch.selected_path()
        target_dir.mkdir(parents=True, exist_ok=True)
        project_dir = target_dir / name
        if project_dir.exists():
            QMessageBox.warning(self, "Existiert schon", f"{project_dir} gibt es bereits.")
            return

        stack = self.stack_combo.currentText()
        packages = [
            p.strip() for p in self.packages_edit.toPlainText().splitlines() if p.strip()
        ]
        use_docker = self.docker_checkbox.isChecked()
        base_image = self.base_image_edit.text().strip() or STACK_PRESETS[stack]["base_image"]
        port = self.port_spin.value()
        env_vars = self._collect_env_vars() if use_docker else {}
        db = self.db_combo.currentText() if self.db_combo.isVisible() else "Keine"

        self.buttons.setEnabled(False)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            create_project(
                project_dir=project_dir,
                stack=stack,
                packages=packages,
                use_docker=use_docker,
                base_image=base_image,
                port=port,
                env_vars=env_vars,
                db=db,
            )
        except FileNotFoundError as exc:
            QMessageBox.critical(
                self, "Werkzeug fehlt",
                f"Ein benötigtes Kommando wurde nicht gefunden: {exc}\n\n"
                "Für React/Vue/Angular werden Node.js + npm/npx im PATH gebraucht."
            )
            return
        except subprocess.CalledProcessError as exc:
            QMessageBox.critical(
                self, "Scaffolding fehlgeschlagen",
                f"Der Generator ist mit Fehler {exc.returncode} abgebrochen.\n"
                f"{exc.stderr.decode(errors='ignore') if exc.stderr else ''}"
            )
            return
        except Exception as exc:  # bewusst breit — Dialog soll nicht crashen
            QMessageBox.critical(self, "Fehler beim Anlegen", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
            self.buttons.setEnabled(True)

        if self.open_vscode_checkbox.isChecked():
            try:
                subprocess.Popen(["code", str(project_dir)])
            except FileNotFoundError:
                QMessageBox.information(
                    self, "Hinweis",
                    "Projekt wurde angelegt, aber 'code' ist nicht im PATH — "
                    "VS Code manuell öffnen."
                )

        self.created_path = project_dir
        self.accept()


# --------------------------------------------------------------------------
# Datei-/Projektgenerierung
# --------------------------------------------------------------------------

def _csharp_namespace(name: str) -> str:
    """Leitet den Root-Namespace so ab, wie `dotnet new` es aus dem
    Ordnernamen macht: ungültige Zeichen (z.B. Bindestriche) -> Unterstrich,
    führende Ziffer -> Unterstrich davor."""
    ns = re.sub(r"[^0-9A-Za-z_]", "_", name)
    if ns and ns[0].isdigit():
        ns = "_" + ns
    return ns or "App"


def _dotnet_connection_string(db: str, use_docker: bool, project_dir: Path) -> str:
    if db == "SQLite":
        return "Data Source=app.db"

    if use_docker and db in DB_SERVICE_PRESETS:
        preset = DB_SERVICE_PRESETS[db]
        host = f"{project_dir.name}-db"
        if db == "PostgreSQL":
            return (
                f"Host={host};Port={preset['port']};"
                f"Database={preset['env']['POSTGRES_DB']};"
                f"Username={preset['env']['POSTGRES_USER']};"
                f"Password={preset['env']['POSTGRES_PASSWORD']}"
            )
        if db == "MySQL":
            return (
                f"Server={host};Port={preset['port']};"
                f"Database={preset['env']['MYSQL_DATABASE']};"
                f"Uid=root;Pwd={preset['env']['MYSQL_ROOT_PASSWORD']}"
            )
        if db == "SQL Server":
            return (
                f"Server={host},{preset['port']};Database=app;"
                f"User Id=sa;Password={preset['env']['MSSQL_SA_PASSWORD']};"
                "TrustServerCertificate=True"
            )

    # Kein Docker -> es gibt keinen bekannten Hostnamen/Credentials, hier
    # kann nur ein Platzhalter für localhost stehen, den der User anpassen
    # muss (siehe CHANGE_ME).
    if db == "PostgreSQL":
        return "Host=localhost;Port=5432;Database=app;Username=postgres;Password=CHANGE_ME"
    if db == "MySQL":
        return "Server=localhost;Port=3306;Database=app;Uid=root;Pwd=CHANGE_ME"
    if db == "SQL Server":
        return (
            "Server=localhost,1433;Database=app;User Id=sa;"
            "Password=CHANGE_ME;TrustServerCertificate=True"
        )
    return ""


def _wire_dotnet_db(project_dir: Path, db: str, use_docker: bool) -> None:
    """Erzeugt Data/AppDbContext.cs, trägt die Connection-String in
    appsettings.json ein und registriert den DbContext in Program.cs.
    Best effort: funktioniert für die Standard-Top-Level-Statement-Vorlagen
    von `dotnet new webapi/webapp/mvc`. Bei stark angepassten Templates
    kann Program.cs manuell nachjustiert werden müssen."""
    namespace = _csharp_namespace(project_dir.name)
    conn_str = _dotnet_connection_string(db, use_docker, project_dir)

    # --- AppDbContext.cs -------------------------------------------------
    data_dir = project_dir / "Data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "AppDbContext.cs").write_text(
        "using Microsoft.EntityFrameworkCore;\n\n"
        f"namespace {namespace}.Data;\n\n"
        "public class AppDbContext : DbContext\n"
        "{\n"
        "    public AppDbContext(DbContextOptions<AppDbContext> options) : base(options)\n"
        "    {\n"
        "    }\n\n"
        "    // DbSets für deine Entities hier ergänzen, z.B.:\n"
        "    // public DbSet<Product> Products => Set<Product>();\n"
        "}\n"
    )

    # --- appsettings.json --------------------------------------------------
    appsettings_path = project_dir / "appsettings.json"
    if appsettings_path.exists():
        settings = json.loads(appsettings_path.read_text())
    else:
        settings = {}
    settings.setdefault("ConnectionStrings", {})["DefaultConnection"] = conn_str
    appsettings_path.write_text(json.dumps(settings, indent=2) + "\n")

    # --- Program.cs: usings + AddDbContext einhängen ------------------------
    program_path = project_dir / "Program.cs"
    if not program_path.exists():
        return  # unbekanntes Template-Layout — hier brechen wir lieber sauber ab

    if db == "MySQL":
        options_call = (
            'options.UseMySql(builder.Configuration.GetConnectionString("DefaultConnection"), '
            'ServerVersion.AutoDetect(builder.Configuration.GetConnectionString("DefaultConnection")))'
        )
    else:
        method = {"SQLite": "UseSqlite", "PostgreSQL": "UseNpgsql", "SQL Server": "UseSqlServer"}[db]
        options_call = f'options.{method}(builder.Configuration.GetConnectionString("DefaultConnection"))'

    usings = ["Microsoft.EntityFrameworkCore", f"{namespace}.Data"]
    if db == "PostgreSQL":
        usings.append("Npgsql.EntityFrameworkCore.PostgreSQL")

    lines = program_path.read_text().splitlines()
    using_lines = [f"using {u};" for u in usings] + [""]
    new_lines = using_lines + lines

    builder_idx = next(
        (i for i, line in enumerate(new_lines) if "WebApplication.CreateBuilder(args)" in line),
        None,
    )
    if builder_idx is None:
        return  # kein Top-Level-Statement-Template erkannt — nichts einhängen

    db_context_block = [
        "",
        "builder.Services.AddDbContext<AppDbContext>(options =>",
        f"    {options_call});",
    ]
    new_lines[builder_idx + 1:builder_idx + 1] = db_context_block
    program_path.write_text("\n".join(new_lines) + "\n")


def _patch_csproj_allow_missing_prune_data(project_dir: Path) -> None:
    """Workaround für NETSDK1226 auf bleeding-edge-SDKs (z.B. CachyOS'
    rolling-release dotnet-sdk-Paket): die Package-Pruning-Referenzdaten
    für den exakten Patch-Stand sind auf NuGet.org manchmal noch nicht
    synchron. Ohne diesen Fix bricht `dotnet add package` sofort ab."""
    csproj_files = list(project_dir.glob("*.csproj"))
    if not csproj_files:
        return
    csproj_path = csproj_files[0]
    content = csproj_path.read_text()
    if "AllowMissingPrunePackageData" in content:
        return  # schon gesetzt, nichts zu tun
    content = content.replace(
        "</PropertyGroup>",
        "    <AllowMissingPrunePackageData>true</AllowMissingPrunePackageData>\n  </PropertyGroup>",
        1,
    )
    csproj_path.write_text(content)


def create_project(
    *,
    project_dir: Path,
    stack: str,
    packages: list[str],
    use_docker: bool,
    base_image: str,
    port: int,
    env_vars: dict[str, str],
    db: str = "Keine",
) -> None:
    preset = STACK_PRESETS[stack]
    kind = preset["kind"]

    # DB-Pakete VOR der Grundgerüst-Erzeugung mergen — die landen in
    # requirements.txt / package.json bzw. werden per `dotnet add` gezogen,
    # das passiert weiter unten mit der fertigen `packages`-Liste.
    if db != "Keine":
        for pkg in DB_PACKAGES.get(kind, {}).get(db, []):
            if pkg not in packages:
                packages.append(pkg)

    # --- Grundgerüst erzeugen -------------------------------------------
    if kind == "manual-python":
        project_dir.mkdir(parents=True)
        (project_dir / "requirements.txt").write_text(
            "\n".join(packages) + ("\n" if packages else "")
        )
        (project_dir / "app.py").write_text(
            '"""Einstiegspunkt."""\n\n'
            'def main() -> None:\n'
            '    print("Hallo aus dem neuen Projekt!")\n\n\n'
            'if __name__ == "__main__":\n'
            '    main()\n'
        )
    elif kind == "manual-node":
        project_dir.mkdir(parents=True)
        package_json = {
            "name": project_dir.name,
            "version": "0.1.0",
            "main": "index.js",
            "scripts": {"start": "node index.js"},
            "dependencies": {pkg: "*" for pkg in packages},
        }
        (project_dir / "package.json").write_text(json.dumps(package_json, indent=2))
        (project_dir / "index.js").write_text('console.log("Hallo aus dem neuen Projekt!");\n')
    elif kind == "manual-other":
        project_dir.mkdir(parents=True)
        (project_dir / "README.md").write_text(
            f"# {project_dir.name}\n\nPakete: {', '.join(packages) or '-'}\n"
        )
    elif kind == "npx":
        # WICHTIG: project_dir NICHT vorher anlegen — der jeweilige
        # CLI-Generator (create-vite / @angular/cli) legt den Ordner
        # selbst an und bricht ab, wenn er schon existiert.
        cmd = [part.format(name=project_dir.name) for part in preset["scaffold_cmd"]]
        subprocess.run(
            cmd, cwd=project_dir.parent, check=True, capture_output=True,
        )
        if packages:
            subprocess.run(
                ["npm", "install", *packages], cwd=project_dir, check=True, capture_output=True,
            )
    elif kind == "dotnet":
        # dotnet new -o {name} legt den Ordner ebenfalls selbst an.
        cmd = [part.format(name=project_dir.name) for part in preset["scaffold_cmd"]]
        subprocess.run(
            cmd, cwd=project_dir.parent, check=True, capture_output=True,
        )
        _patch_csproj_allow_missing_prune_data(project_dir)
        for pkg in packages:
            subprocess.run(
                ["dotnet", "add", str(project_dir), "package", pkg],
                check=True, capture_output=True,
            )
        if db != "Keine":
            _wire_dotnet_db(project_dir, db, use_docker)
    else:
        raise ValueError(f"Unbekannter Preset-Typ: {kind}")

    # --- Docker ----------------------------------------------------------
    if use_docker:
        install_line = ""
        if kind == "manual-python":
            install_line = "RUN pip install --no-cache-dir -r requirements.txt"
        elif kind in ("manual-node", "npx"):
            install_line = "RUN npm install"
        elif kind == "dotnet":
            install_line = "RUN dotnet restore"

        run_parts = preset["run_cmd"].split()
        cmd_json = ", ".join(f'"{part}"' for part in run_parts)

        dockerfile = f"""FROM {base_image}
WORKDIR /app
COPY . /app
{install_line}
EXPOSE {port}
CMD [{cmd_json}]
"""
        (project_dir / "Dockerfile").write_text(dockerfile)

        # Bei einer server-basierten DB (nicht SQLite) automatisch
        # Verbindungs-Env-Vars ergänzen — eigene, vom User gesetzte Werte
        # haben immer Vorrang (setdefault statt Überschreiben).
        effective_env_vars = dict(env_vars)
        db_service_name = f"{project_dir.name}-db"
        db_preset = DB_SERVICE_PRESETS.get(db)
        if db_preset is not None:
            effective_env_vars.setdefault("DB_HOST", db_service_name)
            effective_env_vars.setdefault("DB_PORT", str(db_preset["port"]))
            for k, v in db_preset["env"].items():
                effective_env_vars.setdefault(k, v)

        env_lines = "\n".join(f"{k}={v}" for k, v in effective_env_vars.items())
        (project_dir / ".env").write_text(env_lines + ("\n" if env_lines else ""))
        (project_dir / ".env.example").write_text(
            "\n".join(f"{k}=" for k in effective_env_vars) + ("\n" if effective_env_vars else "")
        )

        compose = {
            "services": {
                project_dir.name: {
                    "build": ".",
                    "ports": [f"{port}:{port}"],
                    "env_file": [".env"],
                    "volumes": [".:/app"],
                }
            }
        }
        if db_preset is not None:
            compose["services"][project_dir.name]["depends_on"] = [db_service_name]
            compose["services"][db_service_name] = {
                "image": db_preset["image"],
                "environment": db_preset["env"],
                "ports": [f'{db_preset["port"]}:{db_preset["port"]}'],
            }
        (project_dir / "docker-compose.yml").write_text(_dict_to_yaml(compose))

        devcontainer_dir = project_dir / ".devcontainer"
        devcontainer_dir.mkdir(exist_ok=True)
        devcontainer = {
            "name": project_dir.name,
            "dockerComposeFile": "../docker-compose.yml",
            "service": project_dir.name,
            "workspaceFolder": "/app",
        }
        (devcontainer_dir / "devcontainer.json").write_text(json.dumps(devcontainer, indent=2))

    # --- VS-Code-Settings --------------------------------------------------
    vscode_dir = project_dir / ".vscode"
    vscode_dir.mkdir(exist_ok=True)
    (vscode_dir / "settings.json").write_text(json.dumps({
        "files.exclude": {"**/__pycache__": True, "**/node_modules": True},
    }, indent=2))

    # --- Git ---------------------------------------------------------------
    gitignore_path = project_dir / ".gitignore"
    extra_ignores = ["venv/", "node_modules/", "__pycache__/", ".env", "dist/", ".angular/"]
    existing = gitignore_path.read_text().splitlines() if gitignore_path.exists() else []
    merged = existing + [line for line in extra_ignores if line not in existing]
    gitignore_path.write_text("\n".join(merged) + "\n")

    if not (project_dir / ".git").exists():
        try:
            subprocess.run(["git", "init"], cwd=project_dir, check=True, capture_output=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass  # Git optional — Projekt trotzdem nutzbar


def _dict_to_yaml(data: dict, indent: int = 0) -> str:
    """Minimaler YAML-Writer ohne externe Abhängigkeit (reicht für docker-compose)."""
    lines = []
    pad = "  " * indent
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"{pad}{key}:")
            lines.append(_dict_to_yaml(value, indent + 1))
        elif isinstance(value, list):
            lines.append(f"{pad}{key}:")
            for item in value:
                lines.append(f"{pad}  - {item}")
        else:
            lines.append(f"{pad}{key}: {value}")
    return "\n".join(lines) + "\n" if indent == 0 else "\n".join(lines)


# --------------------------------------------------------------------------
# Datei-Operationen: Clipboard, Papierkorb, Vorschau
# --------------------------------------------------------------------------

class Clipboard:
    """Sehr simples geteiltes Clipboard-Objekt — beide Panes bekommen
    dieselbe Instanz, damit "Kopieren im linken Pane, Einfügen im
    rechten" funktioniert."""

    def __init__(self):
        self.paths: list[Path] = []
        self.mode: str = ""  # "copy" oder "cut"


def move_to_trash(path: Path) -> None:
    """Verschiebt nach ~/.local/share/Trash gemäß freedesktop.org
    Trash-Spec (vereinfacht — reicht für den Alltag, kein Restore-UI)."""
    trash_files_dir = Path.home() / ".local" / "share" / "Trash" / "files"
    trash_info_dir = Path.home() / ".local" / "share" / "Trash" / "info"
    trash_files_dir.mkdir(parents=True, exist_ok=True)
    trash_info_dir.mkdir(parents=True, exist_ok=True)

    dest = trash_files_dir / path.name
    counter = 1
    while dest.exists():
        dest = trash_files_dir / f"{path.stem}_{counter}{path.suffix}"
        counter += 1

    shutil.move(str(path), str(dest))
    info_content = (
        "[Trash Info]\n"
        f"Path={path}\n"
        f"DeletionDate={datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\n"
    )
    (trash_info_dir / f"{dest.name}.trashinfo").write_text(info_content)


def unique_destination(dest: Path) -> Path:
    """Hängt bei Namenskonflikt " (Kopie N)" an, statt zu überschreiben."""
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    counter = 1
    while True:
        candidate = dest.with_name(f"{stem} (Kopie {counter}){suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


OFFICE_MIME_TYPES = {
    # Modern (OOXML)
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # docx
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # xlsx
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # pptx
    # Legacy Microsoft
    "application/msword",  # doc
    "application/vnd.ms-excel",  # xls
    "application/vnd.ms-powerpoint",  # ppt
    # OpenDocument
    "application/vnd.oasis.opendocument.text",  # odt
    "application/vnd.oasis.opendocument.spreadsheet",  # ods
    "application/vnd.oasis.opendocument.presentation",  # odp
}


def convert_to_pdf_via_libreoffice(path: Path, outdir: Path) -> Path | None:
    """Konvertiert Office-Dateien headless zu PDF (LibreOffice muss
    installiert sein: `sudo pacman -S libreoffice-fresh`). Dauert je nach
    Dateigröße/Komplexität ein paar Sekunden — läuft synchron, das UI
    steht in der Zeit still (siehe Wait-Cursor beim Aufruf)."""
    subprocess.run(
        ["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(outdir), str(path)],
        check=True, capture_output=True, timeout=60,
    )
    candidate = outdir / f"{path.stem}.pdf"
    return candidate if candidate.exists() else None


class ThumbnailIconProvider(QFileIconProvider):
    """Standard-Icon-Provider von Qt liefert für ALLES nur generische
    Mimetype-Icons aus dem System-Theme (Breeze usw.) — auch für Bilder.
    Für Bilder generieren wir stattdessen ein echtes Content-Thumbnail;
    für alles andere bleibt exakt das System-Icon erhalten (super().icon()),
    genau das war der Wunsch: System-Icons behalten, nur Bilder als
    echte Vorschau, konsistent — nicht wie bei Dolphin, wo das je nach
    Ansicht/Cache-Zustand unterschiedlich ausfällt."""

    THUMBNAIL_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "ico"}
    THUMBNAIL_SIZE = 96

    def __init__(self):
        super().__init__()
        self._cache: dict[str, QIcon] = {}

    def icon(self, info) -> QIcon:  # noqa: A003 — Qt-API-Name
        # QFileIconProvider.icon() hat zwei C++-Overloads: einen mit
        # QFileInfo (Normalfall bei jeder Zeile im Modell), einen mit
        # reinem IconType-Enum (generische Icons ohne Datei-Bezug, z.B.
        # in manchen Dialogen). Nur der erste Fall hat .isFile() usw. —
        # beim zweiten sofort an die Basisklasse durchreichen.
        try:
            is_image_file = info.isFile() and info.suffix().lower() in self.THUMBNAIL_EXTENSIONS
        except AttributeError:
            return super().icon(info)

        if is_image_file:
            cache_key = f"{info.absoluteFilePath()}::{info.lastModified().toMSecsSinceEpoch()}"
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

            reader = QImageReader(info.absoluteFilePath())
            reader.setAutoTransform(True)
            size = reader.size()
            if size.isValid():
                reader.setScaledSize(
                    size.scaled(self.THUMBNAIL_SIZE, self.THUMBNAIL_SIZE, Qt.KeepAspectRatio)
                )
            image = reader.read()
            if not image.isNull():
                icon = QIcon(QPixmap.fromImage(image))
                self._cache[cache_key] = icon
                return icon
            # Bild kaputt/nicht lesbar -> Fallback aufs System-Icon unten

        return super().icon(info)


class PreviewDialog(QDialog):
    """F3 — Vorschau: Bild wird gerendert, PDF-Seite 1 wird gerendert,
    Office-Dateien (docx/xlsx/pptx/doc/xls/ppt/odt/ods/odp) werden per
    LibreOffice headless zu PDF konvertiert und dann genauso angezeigt,
    Text wird angezeigt (erste 500 Zeilen), alles andere bekommt nur
    Metadaten."""

    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Vorschau — {path.name}")
        self.resize(700, 550)
        self._temp_dir: Path | None = None

        layout = QVBoxLayout(self)
        mime = QMimeDatabase().mimeTypeForFile(str(path))
        mime_name = mime.name()

        effective_path = path
        effective_mime = mime_name

        if mime_name in OFFICE_MIME_TYPES:
            self._temp_dir = Path(tempfile.mkdtemp(prefix="fm_preview_"))
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                pdf_path = convert_to_pdf_via_libreoffice(path, self._temp_dir)
            except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.CalledProcessError):
                pdf_path = None
            finally:
                QApplication.restoreOverrideCursor()

            if pdf_path is not None and pdf_path.exists():
                effective_path = pdf_path
                effective_mime = "application/pdf"
            else:
                layout.addWidget(QLabel(
                    "Konnte keine Vorschau erzeugen — LibreOffice (soffice) fehlt\n"
                    "oder die Konvertierung ist fehlgeschlagen.\n\n"
                    "Installieren mit: sudo pacman -S libreoffice-fresh",
                    self,
                ))
                effective_mime = None  # weitere Branches überspringen

        if effective_mime is None:
            pass
        elif effective_mime.startswith("image/"):
            pixmap = QPixmap(str(effective_path))
            label = QLabel(self)
            label.setPixmap(
                pixmap.scaled(660, 440, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
            label.setAlignment(Qt.AlignCenter)
            layout.addWidget(label)
        elif effective_mime == "application/pdf":
            doc = QPdfDocument(self)
            doc.load(str(effective_path))
            if doc.status() == QPdfDocument.Status.Ready and doc.pageCount() > 0:
                page_size = doc.pagePointSize(0)  # in Punkten (1/72 Zoll)
                display_width = 660

                # Supersampling: mit doppelter Ziel-Auflösung rendern, dann
                # sauber (SmoothTransformation) auf Anzeigegröße runterskalieren.
                # Ohne das wirkt kleine Schrift bei Direktrender auf 660px
                # blass/grau statt kontrastreich schwarz — Anti-Aliasing-Artefakt.
                render_scale_factor = 2
                scale = (
                    (display_width * render_scale_factor) / page_size.width()
                    if page_size.width() else 1.0
                )
                render_size = QSize(
                    max(1, int(page_size.width() * scale)),
                    max(1, int(page_size.height() * scale)),
                )
                image = doc.render(0, render_size)

                # QPdfDocument.render liefert ein Bild MIT Alpha-Kanal —
                # ohne weißen Seitenhintergrund sieht das auf dunklen
                # KDE-Themes verwaschen/grau aus, weil die transparenten
                # Kanten mit dem dunklen Fensterhintergrund verschmelzen.
                # Erst auf weiß compositen, dann erst runterskalieren.
                opaque_image = QPixmap(render_size)
                opaque_image.fill(Qt.white)
                painter = QPainter(opaque_image)
                painter.drawImage(0, 0, image)
                painter.end()

                display_height = int(display_width * page_size.height() / page_size.width()) \
                    if page_size.width() else render_size.height()
                display_pixmap = opaque_image.scaled(
                    display_width, display_height, Qt.KeepAspectRatio, Qt.SmoothTransformation
                )

                label = QLabel(self)
                label.setPixmap(display_pixmap)
                label.setAlignment(Qt.AlignCenter)

                scroll = QScrollArea(self)
                scroll.setWidget(label)
                scroll.setWidgetResizable(False)
                scroll.setAlignment(Qt.AlignCenter)
                layout.addWidget(scroll)

                caption_text = f"Seite 1 von {doc.pageCount()}"
                if doc.pageCount() > 1:
                    caption_text += " — nur erste Seite, Rest via Doppelklick/xdg-open"
                layout.addWidget(QLabel(caption_text, self))
            else:
                layout.addWidget(QLabel("PDF konnte nicht gerendert werden.", self))
        elif effective_mime.startswith("text/") or mime.inherits("text/plain"):
            try:
                text = effective_path.read_text(errors="replace")
                preview_text = "\n".join(text.splitlines()[:500])
            except Exception as exc:
                preview_text = f"Konnte nicht gelesen werden: {exc}"
            edit = QPlainTextEdit(self)
            edit.setReadOnly(True)
            edit.setPlainText(preview_text)
            edit.setFont(QFont("monospace"))
            layout.addWidget(edit)
        else:
            stat = path.stat()
            modified = datetime.fromtimestamp(stat.st_mtime).strftime("%d.%m.%Y %H:%M")
            info = QLabel(
                f"Keine Vorschau verfügbar für Typ: {mime_name or 'unbekannt'}\n\n"
                f"Größe: {stat.st_size:,} Bytes\n"
                f"Geändert: {modified}",
                self,
            )
            layout.addWidget(info)

        buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def closeEvent(self, event) -> None:
        if self._temp_dir is not None and self._temp_dir.exists():
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        super().closeEvent(event)


# --------------------------------------------------------------------------
# Breadcrumb + Pane
# --------------------------------------------------------------------------

class BreadcrumbBar(QWidget):
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


# --------------------------------------------------------------------------
# Views mit Drag & Drop
# --------------------------------------------------------------------------

class _DropTargetMixin:
    """Gemeinsame Drag&Drop-Logik für Listen- und Kachelansicht.

    QFileSystemModel bringt zwar eigene Drop-Behandlung mit, kann dabei
    aber nur verschieben (kein Kopieren) und weiß nichts vom jeweils
    anderen Pane. Deshalb hier komplett selbst behandelt — dropEvent()
    ruft bewusst kein super() auf, sondern übernimmt Kopieren/Verschieben
    über dieselbe Logik wie Ausschneiden/Einfügen (fm_pane.drop_items).
    """

    fm_pane: "FilePane"  # von FilePane direkt nach dem Erzeugen gesetzt

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        mime = event.mimeData()
        if not mime.hasUrls():
            event.ignore()
            return

        sources = [Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile()]
        sources = [p for p in sources if p.exists()]
        if not sources:
            event.ignore()
            return

        index = self.indexAt(event.position().toPoint())
        target_dir = self.fm_pane.current_path
        if index.isValid():
            path = Path(self.fm_pane.model.filePath(index))
            if path.is_dir():
                target_dir = path

        copy_mode = bool(event.modifiers() & Qt.ControlModifier)
        self.fm_pane.drop_items(sources, target_dir, copy=copy_mode)
        event.acceptProposedAction()


class FileTreeView(_DropTargetMixin, QTreeView):
    pass


class FileListView(_DropTargetMixin, QListView):
    pass


class FilePane(QWidget):
    def __init__(self, start_path: Path, status_callback, clipboard: Clipboard, parent=None):
        super().__init__(parent)
        self.current_path = start_path
        self._status_callback = status_callback
        self.clipboard = clipboard
        self.sibling: "FilePane | None" = None  # anderes Pane, für Auto-Refresh

        self.model = QFileSystemModel(self)
        self.model.setRootPath(QDir.rootPath())
        self.model.setFilter(QDir.AllEntries | QDir.NoDotAndDotDot | QDir.Hidden)
        self.model.setIconProvider(ThumbnailIconProvider())

        # Geteiltes Selection-Model: beide Views (Liste + Kacheln) tragen
        # exakt dieselbe Auswahl mit, auch beim Umschalten dazwischen.
        self.selection_model = QItemSelectionModel(self.model)

        self.view = FileTreeView(self)  # Listenansicht (Details, Spalten)
        self.view.fm_pane = self
        self.view.setModel(self.model)
        self.view.setSelectionModel(self.selection_model)
        self.view.setRootIndex(self.model.index(str(start_path)))
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(0, Qt.AscendingOrder)
        self.view.setAlternatingRowColors(True)
        self.view.setUniformRowHeights(True)
        self.view.setColumnWidth(0, 260)
        self.view.setIconSize(QSize(22, 22))
        self.view.setSelectionMode(QTreeView.ExtendedSelection)
        self.view.setDragEnabled(True)
        self.view.setAcceptDrops(True)
        self.view.setDropIndicatorShown(True)
        self.view.setDragDropMode(QAbstractItemView.DragDrop)
        self.view.doubleClicked.connect(self._on_double_click)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._show_context_menu)

        self.grid_view = FileListView(self)  # Kachelansicht (Icons/Thumbnails)
        self.grid_view.fm_pane = self
        self.grid_view.setModel(self.model)
        self.grid_view.setSelectionModel(self.selection_model)
        self.grid_view.setRootIndex(self.model.index(str(start_path)))
        self.grid_view.setViewMode(QListView.IconMode)
        self.grid_view.setIconSize(QSize(72, 72))
        self.grid_view.setGridSize(QSize(110, 100))
        self.grid_view.setResizeMode(QListView.Adjust)
        self.grid_view.setMovement(QListView.Static)
        self.grid_view.setSpacing(8)
        self.grid_view.setWordWrap(True)
        self.grid_view.setSelectionMode(QListView.ExtendedSelection)
        self.grid_view.setDragEnabled(True)
        self.grid_view.setAcceptDrops(True)
        self.grid_view.setDropIndicatorShown(True)
        self.grid_view.setDragDropMode(QAbstractItemView.DragDrop)
        self.grid_view.doubleClicked.connect(self._on_double_click)
        self.grid_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid_view.customContextMenuRequested.connect(self._show_context_menu)

        self.selection_model.selectionChanged.connect(self._update_status)

        self.view_stack = QStackedWidget(self)
        self.view_stack.addWidget(self.view)       # Index 0 = Liste
        self.view_stack.addWidget(self.grid_view)  # Index 1 = Kacheln

        self.breadcrumb = BreadcrumbBar(self.navigate_to, self)

        # Umschalt-Buttons rechts in der Breadcrumb-Zeile (nach dem Stretch
        # dort platziert -> landen automatisch am rechten Rand).
        self.list_view_btn = QToolButton(self)
        self.list_view_btn.setIcon(QIcon.fromTheme("view-list-details"))
        self.list_view_btn.setToolTip("Listenansicht")
        self.list_view_btn.setCheckable(True)
        self.list_view_btn.setChecked(True)

        self.grid_view_btn = QToolButton(self)
        self.grid_view_btn.setIcon(QIcon.fromTheme("view-list-icons"))
        self.grid_view_btn.setToolTip("Kachelansicht")
        self.grid_view_btn.setCheckable(True)

        view_toggle_group = QButtonGroup(self)
        view_toggle_group.setExclusive(True)
        view_toggle_group.addButton(self.list_view_btn)
        view_toggle_group.addButton(self.grid_view_btn)
        self._view_toggle_group = view_toggle_group  # Referenz halten (GC)

        self.list_view_btn.clicked.connect(lambda: self._set_view_mode("list"))
        self.grid_view_btn.clicked.connect(lambda: self._set_view_mode("grid"))
        self.breadcrumb.layout().addWidget(self.list_view_btn)
        self.breadcrumb.layout().addWidget(self.grid_view_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.breadcrumb)
        layout.addWidget(self.view_stack)

        # WICHTIG: Shortcuts hängen an self (dem ganzen Pane), nicht an
        # self.view — sonst feuern sie nicht mehr, sobald die Kachelansicht
        # den Fokus hat statt der Liste. Kontext bleibt
        # WidgetWithChildrenShortcut: ohne das ist der Default
        # WindowShortcut, und zwei Panes mit identischen Shortcuts im
        # selben Fenster wären für Qt "ambiguous" — dann feuert KEINER
        # von beiden mehr, ohne Fehlermeldung.
        def _bind(key: str, slot):
            sc = QShortcut(QKeySequence(key), self, activated=slot)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            return sc

        _bind("Backspace", self.go_up)
        _bind("F5", self.refresh)
        _bind("F7", self.new_project)
        _bind("F3", self.preview_selection)
        _bind("F2", self.rename_selection)
        _bind("Delete", self.delete_selection)
        _bind("Ctrl+C", self.copy_selection)
        _bind("Ctrl+X", self.cut_selection)
        _bind("Ctrl+V", self.paste)

        self.navigate_to(start_path)

    def _active_view(self):
        return self.grid_view if self.view_stack.currentWidget() is self.grid_view else self.view

    def _set_view_mode(self, mode: str) -> None:
        if mode == "grid":
            self.view_stack.setCurrentWidget(self.grid_view)
            self.grid_view_btn.setChecked(True)
        else:
            self.view_stack.setCurrentWidget(self.view)
            self.list_view_btn.setChecked(True)

    def navigate_to(self, path: Path) -> None:
        if not path.is_dir():
            return
        self.current_path = path
        index = self.model.index(str(path))
        self.view.setRootIndex(index)
        self.grid_view.setRootIndex(index)
        self.breadcrumb.set_path(path)
        self._update_status()

    def go_up(self) -> None:
        parent = self.current_path.parent
        if parent != self.current_path:
            self.navigate_to(parent)

    def refresh(self) -> None:
        self.navigate_to(self.current_path)

    def new_project(self) -> None:
        dialog = NewProjectDialog(self.current_path, self)
        if dialog.exec() == QDialog.Accepted:
            self.refresh()

    def _on_double_click(self, index: QModelIndex) -> None:
        path = Path(self.model.filePath(index))
        if path.is_dir():
            self.navigate_to(path)
        else:
            open_with_default_app(path)

    def _update_status(self) -> None:
        total = self.model.rowCount(self.view.rootIndex())
        selected = len(self.selection_model.selectedRows())
        self._status_callback(f"{selected} von {total} ausgewählt — {self.current_path}")

    def focus_breadcrumb_edit(self) -> None:
        self.breadcrumb.focus_edit()

    # ----------------------------------------------------------------
    # Auswahl-Helper
    # ----------------------------------------------------------------

    def _selected_paths(self) -> list[Path]:
        indexes = self.selection_model.selectedRows()
        return [Path(self.model.filePath(idx)) for idx in indexes]

    def _refresh_all(self) -> None:
        """Aktualisiert dieses Pane und (falls vorhanden) das andere —
        wichtig nach Operationen, die ein Verzeichnis treffen könnten,
        das gerade im jeweils anderen Pane angezeigt wird (v.a. Drag&Drop
        zwischen den Panes)."""
        self.refresh()
        if self.sibling is not None:
            self.sibling.refresh()

    # ----------------------------------------------------------------
    # Copy / Cut / Paste / Drag & Drop
    # ----------------------------------------------------------------

    def copy_selection(self) -> None:
        paths = self._selected_paths()
        if not paths:
            return
        self.clipboard.paths = paths
        self.clipboard.mode = "copy"
        self._status_callback(f"{len(paths)} Objekt(e) zum Kopieren gemerkt")

    def cut_selection(self) -> None:
        paths = self._selected_paths()
        if not paths:
            return
        self.clipboard.paths = paths
        self.clipboard.mode = "cut"
        self._status_callback(f"{len(paths)} Objekt(e) zum Verschieben gemerkt")

    def paste(self) -> None:
        if not self.clipboard.paths:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        errors: list[str] = []
        try:
            for src in self.clipboard.paths:
                if not src.exists():
                    continue
                dest = unique_destination(self.current_path / src.name)
                try:
                    if self.clipboard.mode == "copy":
                        if src.is_dir():
                            shutil.copytree(src, dest)
                        else:
                            shutil.copy2(src, dest)
                    elif self.clipboard.mode == "cut":
                        shutil.move(str(src), str(dest))
                except Exception as exc:
                    errors.append(f"{src.name}: {exc}")
        finally:
            QApplication.restoreOverrideCursor()

        if self.clipboard.mode == "cut" and not errors:
            self.clipboard.paths = []
            self.clipboard.mode = ""

        self._refresh_all()
        if errors:
            QMessageBox.warning(self, "Teilweise fehlgeschlagen", "\n".join(errors))

    def drop_items(self, sources: list[Path], target_dir: Path, copy: bool) -> None:
        """Ziel eines Drag&Drop-Vorgangs: kopiert oder verschiebt sources
        nach target_dir. Strg beim Loslassen = Kopieren, sonst
        Verschieben (analog Dolphin/Nautilus)."""
        if not sources:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        errors: list[str] = []
        try:
            for src in sources:
                if not src.exists():
                    continue
                if target_dir == src or src in target_dir.parents:
                    errors.append(f"{src.name}: Ziel liegt innerhalb der Quelle")
                    continue
                if src.parent == target_dir and not copy:
                    continue  # bereits hier abgelegt -> nichts zu tun
                dest = unique_destination(target_dir / src.name)
                try:
                    if copy:
                        if src.is_dir():
                            shutil.copytree(src, dest)
                        else:
                            shutil.copy2(src, dest)
                    else:
                        shutil.move(str(src), str(dest))
                except Exception as exc:
                    errors.append(f"{src.name}: {exc}")
        finally:
            QApplication.restoreOverrideCursor()

        self._refresh_all()
        if errors:
            QMessageBox.warning(self, "Teilweise fehlgeschlagen", "\n".join(errors))

    # ----------------------------------------------------------------
    # Löschen (Papierkorb) / Umbenennen / Vorschau
    # ----------------------------------------------------------------

    def delete_selection(self) -> None:
        paths = self._selected_paths()
        if not paths:
            self._status_callback("Löschen: nichts ausgewählt")
            return
        names = ", ".join(p.name for p in paths[:5]) + (" …" if len(paths) > 5 else "")
        reply = QMessageBox.question(
            self, "In den Papierkorb verschieben?",
            f"{len(paths)} Objekt(e) in den Papierkorb verschieben?\n\n{names}",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        errors: list[str] = []
        for p in paths:
            try:
                move_to_trash(p)
            except Exception as exc:
                errors.append(f"{p.name}: {exc}")
        self._refresh_all()
        if errors:
            QMessageBox.warning(self, "Teilweise fehlgeschlagen", "\n".join(errors))

    def rename_selection(self) -> None:
        idx = self.selection_model.currentIndex()
        if idx.isValid():
            self._active_view().edit(idx)

    def preview_selection(self) -> None:
        paths = self._selected_paths()
        if not paths or paths[0].is_dir():
            return
        PreviewDialog(paths[0], self).exec()

    def create_folder(self) -> None:
        name, ok = QInputDialog.getText(self, "Neuer Ordner", "Name:")
        if ok and name.strip():
            try:
                (self.current_path / name.strip()).mkdir()
                self._refresh_all()
            except Exception as exc:
                QMessageBox.warning(self, "Fehler", str(exc))

    # ----------------------------------------------------------------
    # Kontextmenü
    # ----------------------------------------------------------------

    def _show_context_menu(self, pos) -> None:
        source_view = self.sender()

        # Standard-Dateimanager-Verhalten: Rechtsklick auf ein Element,
        # das (noch) nicht ausgewählt ist, wählt es aus, statt auf einer
        # evtl. veralteten/verlorenen Auswahl zu operieren. Ohne das
        # konnten Kontextmenü-Aktionen "leer" wirken, wenn die Auswahl
        # zwischenzeitlich nicht sauber im Selection-Model ankam.
        index_at_pos = source_view.indexAt(pos)
        if index_at_pos.isValid() and index_at_pos not in self.selection_model.selectedRows():
            self.selection_model.select(
                index_at_pos,
                QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows,
            )
            self.selection_model.setCurrentIndex(index_at_pos, QItemSelectionModel.Current)

        menu = QMenu(self)
        menu.addAction("Vorschau (F3)", self.preview_selection)
        menu.addSeparator()
        menu.addAction("Ausschneiden (Strg+X)", self.cut_selection)
        menu.addAction("Kopieren (Strg+C)", self.copy_selection)
        paste_action = menu.addAction("Einfügen (Strg+V)", self.paste)
        paste_action.setEnabled(bool(self.clipboard.paths))
        menu.addSeparator()
        menu.addAction("Umbenennen (F2)", self.rename_selection)
        menu.addAction("Löschen (Entf)", self.delete_selection)
        menu.addSeparator()
        menu.addAction("Neuer Ordner", self.create_folder)
        menu.addAction("Neues Projekt (F7)", self.new_project)
        menu.exec(source_view.viewport().mapToGlobal(pos))


# --------------------------------------------------------------------------
# Eingebettetes Terminal (echtes pty + ANSI-Emulation über pyte)
# --------------------------------------------------------------------------
#
# QProcess+Pipes reicht hier nicht: interaktive Programme (vim, htop, less,
# Passwort-Prompts) brauchen ein echtes pty mit Zeilendisziplin, Farben und
# Cursor-Steuerung. Deshalb pty.fork() (echtes Kind-Pty, keine Bibliothek
# nötig) + pyte (reiner Python-VT100/ANSI-Interpreter) für den Bildschirm-
# puffer, den wir selbst als Zeichengitter zeichnen.

_ANSI_BASE = {
    "black": (0, 0, 0), "red": (205, 49, 49), "green": (13, 188, 121),
    "brown": (229, 229, 16), "yellow": (229, 229, 16), "blue": (36, 114, 200),
    "magenta": (188, 63, 188), "cyan": (17, 168, 205), "white": (229, 229, 229),
}
_ANSI_BRIGHT = {
    "black": (102, 102, 102), "red": (241, 76, 76), "green": (35, 209, 139),
    "brown": (245, 245, 67), "yellow": (245, 245, 67), "blue": (59, 142, 234),
    "magenta": (214, 112, 214), "cyan": (41, 184, 219), "white": (255, 255, 255),
}
_ANSI_256_TABLE = (
    [_ANSI_BASE[n] for n in ("black", "red", "green", "brown", "blue", "magenta", "cyan", "white")]
    + [_ANSI_BRIGHT[n] for n in ("black", "red", "green", "brown", "blue", "magenta", "cyan", "white")]
)


def _color_from_256(index: int) -> tuple[int, int, int]:
    if index < 16:
        return _ANSI_256_TABLE[index]
    if index < 232:
        index -= 16
        r, g, b = index // 36, (index % 36) // 6, index % 6
        scale = lambda v: 0 if v == 0 else 55 + v * 40
        return (scale(r), scale(g), scale(b))
    gray = 8 + (index - 232) * 10
    return (gray, gray, gray)


def _resolve_terminal_color(value, bold: bool, default_rgb: tuple[int, int, int]) -> QColor:
    if value in (None, "default"):
        return QColor(*default_rgb)
    if isinstance(value, str) and value.isdigit():
        return QColor(*_color_from_256(int(value)))
    if value in _ANSI_BASE:
        table = _ANSI_BRIGHT if bold else _ANSI_BASE
        return QColor(*table[value])
    return QColor(*default_rgb)


_TERMINAL_KEY_SEQUENCES = {
    Qt.Key_Up: b"\x1b[A",
    Qt.Key_Down: b"\x1b[B",
    Qt.Key_Right: b"\x1b[C",
    Qt.Key_Left: b"\x1b[D",
    Qt.Key_Home: b"\x1b[H",
    Qt.Key_End: b"\x1b[F",
    Qt.Key_Insert: b"\x1b[2~",
    Qt.Key_Delete: b"\x1b[3~",
    Qt.Key_PageUp: b"\x1b[5~",
    Qt.Key_PageDown: b"\x1b[6~",
    Qt.Key_Backspace: b"\x7f",
    Qt.Key_Tab: b"\t",
    Qt.Key_Return: b"\r",
    Qt.Key_Enter: b"\r",
    Qt.Key_Escape: b"\x1b",
}


class TerminalWidget(QWidget):
    """Ein Terminal-Emulator-Widget: echte Shell über pty.fork(), Bildschirm-
    interpretation über pyte, Rendering von Hand (kein fertiges Qt-Terminal-
    Widget für PySide6 verfügbar)."""

    def __init__(self, start_path: Path, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setCursor(Qt.IBeamCursor)

        self._font = QFont("DejaVu Sans Mono")
        self._font.setStyleHint(QFont.Monospace)
        self._font.setPointSize(11)
        self._bold_font = QFont(self._font)
        self._bold_font.setBold(True)
        metrics = QFontMetrics(self._font)
        self._cell_w = max(1, metrics.horizontalAdvance("M"))
        self._cell_h = max(1, metrics.height())
        self._ascent = metrics.ascent()

        self._bg_rgb = (30, 30, 30)
        self._fg_rgb = (220, 220, 220)
        self._bg_color = QColor(*self._bg_rgb)
        self._cursor_color = QColor(255, 255, 255, 140)

        self.screen = pyte.HistoryScreen(80, 24, history=4000, ratio=0.5)
        self.stream = pyte.Stream(self.screen)

        self.master_fd: int | None = None
        self.pid: int | None = None
        self._alive = False
        self._notifier: QSocketNotifier | None = None

        self.start_shell(start_path)

    # ------------------------------------------------------------
    # Prozess-Lebenszyklus
    # ------------------------------------------------------------

    def is_alive(self) -> bool:
        return self._alive

    def start_shell(self, cwd: Path) -> None:
        if self._alive:
            return
        pid, master_fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(str(cwd))
            except OSError:
                pass
            os.environ["TERM"] = "xterm-256color"
            shell = os.environ.get("SHELL", "/bin/bash")
            try:
                os.execvp(shell, [shell])
            except OSError:
                os.execvp("/bin/sh", ["/bin/sh"])
            os._exit(1)  # nur falls exec fehlschlägt

        self.pid = pid
        self.master_fd = master_fd
        fcntl.fcntl(master_fd, fcntl.F_SETFL, os.O_NONBLOCK)
        self._alive = True
        self._notifier = QSocketNotifier(master_fd, QSocketNotifier.Read, self)
        self._notifier.activated.connect(self._on_master_ready)
        self._resize_pty(self.screen.lines, self.screen.columns)
        self.update()

    def shutdown(self) -> None:
        """Beim Schließen des Hauptfensters: Shell (inkl. evtl. laufender
        Kindprozesse wie Editoren) sauber beenden statt sie verwaist im
        Hintergrund weiterlaufen zu lassen."""
        if self._alive and self.pid:
            try:
                os.killpg(os.getpgid(self.pid), signal.SIGHUP)
                os.waitpid(self.pid, 0)  # blockierend: Shell beendet sich binnen Millisekunden
            except (ProcessLookupError, PermissionError, ChildProcessError, OSError):
                pass
        self._teardown()

    def _teardown(self) -> None:
        self._alive = False
        if self._notifier is not None:
            self._notifier.setEnabled(False)
            self._notifier = None
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        if self.pid is not None:
            try:
                os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                pass

    def _on_master_ready(self) -> None:
        if not self._alive or self.master_fd is None:
            return
        try:
            data = os.read(self.master_fd, 65536)
        except OSError:
            data = b""
        if not data:
            self._teardown()
            self.stream.feed(
                "\r\n[Shell beendet — F4 schließt das Panel, "
                "erneutes Öffnen startet eine neue Shell.]\r\n"
            )
            self.update()
            return
        self.stream.feed(data.decode("utf-8", "replace"))
        self.update()

    def _resize_pty(self, rows: int, cols: int) -> None:
        if self.master_fd is None:
            return
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        try:
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
            if self.pid:
                os.kill(self.pid, signal.SIGWINCH)
        except OSError:
            pass

    def cd_to(self, path: Path) -> None:
        if not self._alive or self.master_fd is None:
            return
        os.write(self.master_fd, f"cd {shlex.quote(str(path))}\n".encode())

    # ------------------------------------------------------------
    # Tastatur / Maus
    # ------------------------------------------------------------

    def event(self, event) -> bool:
        # Verhindert, dass fm-weite Shortcuts (Tab = Pane wechseln,
        # Strg+C/X/V, ...) Tastendrücke abfangen, während das Terminal
        # den Fokus hat — die sollen bei der Shell ankommen. F4 bleibt
        # bewusst ausgenommen, damit man das Panel wieder zuklappen kann.
        if event.type() == QEvent.ShortcutOverride:
            if event.key() != Qt.Key_F4:
                event.accept()
                return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:
        if not self._alive or self.master_fd is None:
            return
        key = event.key()
        mods = event.modifiers()

        if mods & Qt.ControlModifier and Qt.Key_A <= key <= Qt.Key_Z:
            os.write(self.master_fd, bytes([key - Qt.Key_A + 1]))
            return

        data = _TERMINAL_KEY_SEQUENCES.get(key)
        if data is not None:
            os.write(self.master_fd, data)
            return

        text = event.text()
        if text:
            os.write(self.master_fd, text.encode("utf-8", "ignore"))

    def wheelEvent(self, event) -> None:
        if event.angleDelta().y() > 0:
            self.screen.prev_page()
        else:
            self.screen.next_page()
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        cols = max(10, self.width() // self._cell_w)
        rows = max(3, self.height() // self._cell_h)
        if (rows, cols) != (self.screen.lines, self.screen.columns):
            self.screen.resize(rows, cols)
            self._resize_pty(rows, cols)

    # ------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._bg_color)
        painter.setFont(self._font)
        cw, ch = self._cell_w, self._cell_h
        buffer = self.screen.buffer

        for row in range(self.screen.lines):
            line = buffer.get(row)
            if not line:
                continue
            for col, char in line.items():
                if col >= self.screen.columns:
                    continue
                fg = _resolve_terminal_color(char.fg, char.bold, self._fg_rgb)
                bg = _resolve_terminal_color(char.bg, False, self._bg_rgb)
                if char.reverse:
                    fg, bg = bg, fg
                x, y = col * cw, row * ch
                if bg != self._bg_color:
                    painter.fillRect(x, y, cw, ch, bg)
                if char.data and char.data != " ":
                    painter.setFont(self._bold_font if char.bold else self._font)
                    painter.setPen(fg)
                    painter.drawText(x, y + self._ascent, char.data)

        if not self.screen.cursor.hidden and self.hasFocus():
            cx, cy = self.screen.cursor.x, self.screen.cursor.y
            painter.fillRect(cx * cw, cy * ch, cw, ch, self._cursor_color)
        painter.end()


class TerminalPanel(QWidget):
    """Terminal-Widget plus schmale Kopfleiste (Titel + 'in aktuellen
    Ordner wechseln'), unten im Hauptfenster ein-/ausblendbar (F4)."""

    def __init__(self, get_active_dir, parent=None):
        super().__init__(parent)
        self._get_active_dir = get_active_dir
        self.terminal = TerminalWidget(get_active_dir(), self)

        header = QWidget(self)
        header.setFixedHeight(22)
        header.setStyleSheet("background-color: #262626;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 0, 4, 0)
        header_layout.setSpacing(2)

        title = QLabel("Terminal", header)
        title.setStyleSheet("color: #888888; font-size: 11px;")

        button_style = """
            QToolButton {
                color: #999999;
                font-size: 11px;
                border: none;
                background: transparent;
                padding: 2px 6px;
            }
            QToolButton:hover {
                color: #eeeeee;
                background: #3a3a3a;
                border-radius: 3px;
            }
        """
        cd_btn = QToolButton(header)
        cd_btn.setText("→ aktueller Ordner")
        cd_btn.setToolTip("cd in den Ordner, der im aktiven Pane offen ist")
        cd_btn.setStyleSheet(button_style)
        cd_btn.clicked.connect(lambda: self.terminal.cd_to(self._get_active_dir()))
        close_btn = QToolButton(header)
        close_btn.setText("✕")
        close_btn.setToolTip("Terminal ausblenden (F4)")
        close_btn.setStyleSheet(button_style)
        close_btn.clicked.connect(lambda: self.setVisible(False))
        header_layout.addWidget(title)
        header_layout.addStretch()
        header_layout.addWidget(cd_btn)
        header_layout.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(self.terminal)

    def ensure_alive(self) -> None:
        if not self.terminal.is_alive():
            self.terminal.start_shell(self._get_active_dir())

    def shutdown(self) -> None:
        self.terminal.shutdown()


class MainWindow(QMainWindow):
    def __init__(self, start_path: Path):
        super().__init__()
        self.setWindowTitle("fm — Dual-Pane + Projekt-Scaffolder")
        self.resize(1400, 800)
        self.setWindowIcon(QIcon.fromTheme("system-file-manager"))

        self.status = QStatusBar(self)
        self.setStatusBar(self.status)
        self.status.showMessage(
            "F7 Neues Projekt · Strg+C/X/V Kopieren/Ausschneiden/Einfügen · "
            "F2 Umbenennen · Entf Löschen · F3 Vorschau · F5 Refresh · "
            "F4 Terminal · Drag&Drop zum Verschieben, Strg+Ziehen zum Kopieren"
        )

        self.clipboard = Clipboard()
        self.left = FilePane(start_path, self._status_from_left, self.clipboard)
        self.right = FilePane(start_path, self._status_from_right, self.clipboard)
        self.left.sibling = self.right
        self.right.sibling = self.left
        self.active_pane = self.left

        pane_splitter = QSplitter(Qt.Horizontal, self)
        pane_splitter.addWidget(self.left)
        pane_splitter.addWidget(self.right)
        pane_splitter.setSizes([700, 700])

        self.terminal_panel = TerminalPanel(lambda: self.active_pane.current_path, self)
        self.terminal_panel.setVisible(False)

        main_splitter = QSplitter(Qt.Vertical, self)
        main_splitter.addWidget(pane_splitter)
        main_splitter.addWidget(self.terminal_panel)
        main_splitter.setSizes([650, 250])
        self.setCentralWidget(main_splitter)

        self.left.view.clicked.connect(lambda _: self._set_active(self.left))
        self.left.grid_view.clicked.connect(lambda _: self._set_active(self.left))
        self.right.view.clicked.connect(lambda _: self._set_active(self.right))
        self.right.grid_view.clicked.connect(lambda _: self._set_active(self.right))

        QShortcut(QKeySequence("Tab"), self, activated=self._toggle_active_pane)
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self._focus_pathbar)
        QShortcut(QKeySequence("F4"), self, activated=self._toggle_terminal)

        self._build_menu_bar()

    def closeEvent(self, event) -> None:
        self.terminal_panel.shutdown()
        super().closeEvent(event)

    def _toggle_terminal(self) -> None:
        visible = not self.terminal_panel.isVisible()
        self.terminal_panel.setVisible(visible)
        if visible:
            self.terminal_panel.ensure_alive()
            self.terminal_panel.terminal.setFocus()
        else:
            self.active_pane._active_view().setFocus()

    # ----------------------------------------------------------------
    # Menüleiste
    # ----------------------------------------------------------------

    def _build_menu_bar(self) -> None:
        # Menü-Einträge binden bewusst KEINE eigenen QKeySequence-Shortcuts
        # (setShortcut) — die Tastenkürzel existieren bereits als
        # WidgetWithChildrenShortcut je Pane (siehe FilePane.__init__).
        # Ein zweiter Shortcut mit derselben Sequenz auf Fenster-Ebene
        # würde Qt als "ambiguous" werten -> beide feuern dann gar nicht
        # mehr. Die "\t..."-Suffixe sind daher rein optische Hinweise.
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("&Datei")
        file_menu.addAction("Neuer Ordner", lambda: self.active_pane.create_folder())
        file_menu.addAction("Neues Projekt …\tF7", lambda: self.active_pane.new_project())
        file_menu.addSeparator()
        file_menu.addAction("Aktualisieren\tF5", lambda: self.active_pane.refresh())
        file_menu.addSeparator()
        quit_action = file_menu.addAction("Beenden\tStrg+Q", self.close)
        quit_action.setShortcut(QKeySequence("Ctrl+Q"))

        edit_menu = menu_bar.addMenu("&Bearbeiten")
        edit_menu.addAction("Ausschneiden\tStrg+X", lambda: self.active_pane.cut_selection())
        edit_menu.addAction("Kopieren\tStrg+C", lambda: self.active_pane.copy_selection())
        edit_menu.addAction("Einfügen\tStrg+V", lambda: self.active_pane.paste())
        edit_menu.addSeparator()
        edit_menu.addAction("Umbenennen\tF2", lambda: self.active_pane.rename_selection())
        edit_menu.addAction("Löschen\tEntf", lambda: self.active_pane.delete_selection())

        view_menu = menu_bar.addMenu("&Ansicht")
        view_menu.addAction("Listenansicht", lambda: self.active_pane._set_view_mode("list"))
        view_menu.addAction("Kachelansicht", lambda: self.active_pane._set_view_mode("grid"))
        view_menu.addSeparator()
        view_menu.addAction("Vorschau\tF3", lambda: self.active_pane.preview_selection())
        view_menu.addSeparator()
        view_menu.addAction("Terminal ein-/ausblenden\tF4", self._toggle_terminal)

        go_menu = menu_bar.addMenu("&Gehe zu")
        go_menu.addAction("Nach oben\tBackspace", lambda: self.active_pane.go_up())
        go_menu.addAction("Pfad eingeben …\tStrg+L", self._focus_pathbar)
        go_menu.addAction("Home", lambda: self.active_pane.navigate_to(Path.home()))
        go_menu.addSeparator()
        go_menu.addAction("Anderes Pane aktivieren\tTab", self._toggle_active_pane)

        help_menu = menu_bar.addMenu("&Hilfe")
        help_menu.addAction("Über fm", self._show_about)

    def _show_about(self) -> None:
        QMessageBox.about(
            self, "Über fm",
            "fm — Dual-Pane Dateimanager mit Projekt-Scaffolder\n\n"
            "Kopieren/Ausschneiden/Einfügen, Drag & Drop (Strg+Ziehen = "
            "Kopieren), Papierkorb, Vorschau und Projekt-Generator (F7).",
        )

    def _set_active(self, pane: FilePane) -> None:
        self.active_pane = pane

    def _toggle_active_pane(self) -> None:
        self.active_pane = self.right if self.active_pane is self.left else self.left
        self.active_pane._active_view().setFocus()

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