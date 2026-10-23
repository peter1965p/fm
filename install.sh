#!/bin/bash
# Als normaler Benutzer ausführen: ./install.sh   (NICHT mit sudo —
# fm ist ein Benutzer-Tool, kein System-Daemon)
set -e
[ "$(id -u)" != 0 ] || { echo "Bitte NICHT als root ausführen." >&2; exit 1; }

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

command -v python3 >/dev/null || { echo "python3 wird benötigt." >&2; exit 1; }
command -v git >/dev/null || echo "Hinweis: git fehlt — automatisches Update funktioniert dann nicht (sudo pacman -S git)" >&2

if [ ! -x venv/bin/python3 ]; then
    echo "Lege venv an …"
    python3 -m venv venv
fi
echo "Installiere/aktualisiere Abhängigkeiten …"
venv/bin/pip install -q -r requirements.txt

chmod +x "$REPO_DIR/fm" "$REPO_DIR/fm-update"

mkdir -p "$HOME/.local/bin"
ln -sf "$REPO_DIR/fm" "$HOME/.local/bin/fm"

mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/fm.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=fm
Comment=Dual-Pane Dateimanager mit Terminal, Archiven und Projekt-Scaffolder
Exec=$REPO_DIR/fm %f
Icon=system-file-manager
Terminal=false
Categories=System;FileTools;FileManager;
EOF

if command -v git >/dev/null; then
    mkdir -p "$HOME/.config/systemd/user"
    cat > "$HOME/.config/systemd/user/fm-update.service" <<EOF
[Unit]
Description=fm — Git-Auto-Update

[Service]
Type=oneshot
ExecStart=$REPO_DIR/fm-update
EOF

    cat > "$HOME/.config/systemd/user/fm-update.timer" <<EOF
[Unit]
Description=Zeitplan für fm-Git-Auto-Update

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable --now fm-update.timer
    echo "Auto-Update-Timer aktiv (täglich, git pull in $REPO_DIR)."
fi

echo "Fertig."
echo "  Start:          fm   (oder aus dem Anwendungsmenü)"
echo "  Update jetzt:   systemctl --user start fm-update.service"
echo "  Update-Log:     journalctl --user -u fm-update"
