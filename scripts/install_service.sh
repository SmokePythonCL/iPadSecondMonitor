#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$REPO_ROOT/linux-server/ipad-monitor.service"
TARGET_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
TARGET_FILE="$TARGET_DIR/ipad-monitor.service"

mkdir -p "$TARGET_DIR"
sed "s|@INSTALL_DIR@|$REPO_ROOT|g" "$TEMPLATE" > "$TARGET_FILE"

echo "Servicio instalado en: $TARGET_FILE"

# Importar variables Wayland/KDE al entorno systemd de usuario actual
systemctl --user import-environment WAYLAND_DISPLAY DISPLAY XDG_CURRENT_DESKTOP 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable ipad-monitor.service
systemctl --user restart ipad-monitor.service

echo ""
python3 "$REPO_ROOT/linux-server/host_daemon.py" --show-pin || true
echo ""
if command -v ufw >/dev/null 2>&1; then
    echo "Nota firewall (UFW): si usas Wi-Fi y está bloqueado, ejecuta:"
    echo "  sudo ufw allow 8889/tcp"
fi
echo "Estado del servicio:"
systemctl --user --no-pager status ipad-monitor.service || true
