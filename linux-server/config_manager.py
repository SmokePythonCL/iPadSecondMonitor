"""User configuration management for iPad Second Monitor."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

DEFAULT_USB_PORT = 8888
DEFAULT_WIFI_PORT = 8889


def get_config_dir() -> Path:
    """Return XDG config directory for ipad-monitor."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "ipad-monitor"


def get_config_path() -> Path:
    """Return path to config.json."""
    return get_config_dir() / "config.json"


def generate_short_pin() -> str:
    """Generate a random 4-digit pairing PIN (avoiding trivial patterns)."""
    while True:
        pin = f"{secrets.randbelow(9000) + 1000}"
        if len(set(pin)) > 1:
            return pin


def load_config() -> dict[str, Any]:
    """Load persisted configuration or initialize defaults with a secure PIN."""
    path = get_config_path()
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}

    changed = False
    if not isinstance(data.get("pin"), str) or not data["pin"]:
        data["pin"] = generate_short_pin()
        changed = True
    if not isinstance(data.get("wifi_port"), int):
        data["wifi_port"] = DEFAULT_WIFI_PORT
        changed = True
    if not isinstance(data.get("usb_port"), int):
        data["usb_port"] = DEFAULT_USB_PORT
        changed = True
    if data.get("mode") not in ("auto", "usb", "wifi"):
        data["mode"] = "auto"
        changed = True

    if changed:
        save_config(data)
    return data


def save_config(data: dict[str, Any]) -> None:
    """Persist config atomically with restrictive owner-only permissions."""
    cfg_dir = get_config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / "config.json"
    tmp_path = cfg_dir / "config.json.tmp"
    tmp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp_path, 0o600)
    tmp_path.replace(path)


def set_pin(new_pin: str) -> dict[str, Any]:
    """Update the Wi-Fi pairing PIN."""
    cleaned = new_pin.strip()
    if len(cleaned) < 4:
        raise ValueError("El PIN debe tener al menos 4 caracteres.")
    cfg = load_config()
    cfg["pin"] = cleaned
    save_config(cfg)
    return cfg


def get_restore_token() -> str | None:
    """Return saved ScreenCast portal restore_token if present and valid for ScreenCast."""
    cfg = load_config()
    if cfg.get("restore_portal") != "screencast":
        return None
    tok = cfg.get("restore_token")
    return tok if isinstance(tok, str) and tok else None


def set_restore_token(token: str | None) -> None:
    """Save or clear the ScreenCast portal restore_token."""
    cfg = load_config()
    if token:
        cfg["restore_token"] = token
        cfg["restore_portal"] = "screencast"
    else:
        cfg.pop("restore_token", None)
        cfg.pop("restore_portal", None)
    save_config(cfg)
