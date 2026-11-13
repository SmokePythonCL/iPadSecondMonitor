#!/bin/bash
set -euo pipefail

TARGET_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/ipad-monitor.service"

systemctl --user disable --now ipad-monitor.service 2>/dev/null || true
rm -f "$TARGET_FILE"
systemctl --user daemon-reload

echo "Servicio ipad-monitor.service desinstalado."
