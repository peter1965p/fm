#!/bin/bash
# Entfernt Menüeintrag, PATH-Symlink und den Auto-Update-Timer.
# Das Repository selbst (inkl. venv, deine Dateien) bleibt unangetastet
# unter ~/Dev/fm liegen.
set -e
[ "$(id -u)" != 0 ] || { echo "Bitte NICHT als root ausführen." >&2; exit 1; }

systemctl --user disable --now fm-update.timer 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/fm-update.service" "$HOME/.config/systemd/user/fm-update.timer"
systemctl --user daemon-reload 2>/dev/null || true

# Vorherigen Standard-Dateimanager wiederherstellen, falls install.sh ihn
# durch fm ersetzt hatte (nur wenn fm seitdem noch aktiv der Standard ist —
# falls der Nutzer zwischendurch manuell was anderes eingestellt hat, nicht
# einfach wieder überschreiben).
if command -v xdg-mime >/dev/null && [ -f "$HOME/.config/fm/previous-default-filemanager" ]; then
    PREV=$(cat "$HOME/.config/fm/previous-default-filemanager")
    CURRENT=$(xdg-mime query default inode/directory 2>/dev/null || true)
    if [ "$CURRENT" = "fm.desktop" ] && [ -n "$PREV" ]; then
        xdg-mime default "$PREV" inode/directory
        echo "Standard-Dateimanager zurückgesetzt auf: $PREV"
    fi
    rm -f "$HOME/.config/fm/previous-default-filemanager"
fi

rm -f "$HOME/.local/bin/fm" "$HOME/.local/share/applications/fm.desktop"

echo "Entfernt. Das Repository selbst bleibt unverändert liegen."
