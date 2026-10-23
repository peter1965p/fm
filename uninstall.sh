#!/bin/bash
# Entfernt Menüeintrag, PATH-Symlink und den Auto-Update-Timer.
# Das Repository selbst (inkl. venv, deine Dateien) bleibt unangetastet
# unter ~/Dev/fm liegen.
set -e
[ "$(id -u)" != 0 ] || { echo "Bitte NICHT als root ausführen." >&2; exit 1; }

systemctl --user disable --now fm-update.timer 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/fm-update.service" "$HOME/.config/systemd/user/fm-update.timer"
systemctl --user daemon-reload 2>/dev/null || true

rm -f "$HOME/.local/bin/fm" "$HOME/.local/share/applications/fm.desktop"

echo "Entfernt. Das Repository selbst bleibt unverändert liegen."
