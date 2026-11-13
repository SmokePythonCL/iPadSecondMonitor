<p align="center">
  <img src="assets/logo.svg" width="96" height="96" alt="iPad Second Monitor logo">
</p>

# iPad Second Monitor

Turn your iPad into a low-latency second monitor for **Linux (Wayland / KDE Plasma)** over **USB (`usbmuxd`)** or **Wi-Fi (LAN)**, with hardware H.264 streaming, event-driven standby, KDE virtual screen persistence, and touch/trackpad gestures.

## Features

- **Dual Connectivity (`USB` + `Wi-Fi LAN`)**
  - **USB**: Connects directly to `/var/run/usbmuxd` (`port 8888`) and monitors `/run` via Linux `inotify` (`IN_CREATE | IN_MOVED_TO`), eliminating `iproxy` respawns and `pkill` loops.
  - **Wi-Fi LAN**: Passive TCP server (`port 8889`) advertised on the local network as `_ipadmonitor._tcp` via `avahi-publish-service` (Bonjour-compatible), with manual IP entry as fallback.
- **Short 4-Digit PIN Wi-Fi Security**
  - Mutual HMAC-SHA256 challenge-response authentication (`OP_AUTH` / `OP_AUTH_RESPONSE`) before opening any capture session, rate-limited after repeated failures, with an optional desktop notification (`notify-send`) showing the current PIN.
- **Standby Resource Optimization**
  - Zero background polling in standby and lazy loading of `gi.repository.Gst`, `GLib`, and `dbus` only when an iPad starts a stream.
- **Hardware H.264 + MJPEG Fallback**
  - Automatically probes encoders in order: `vah264lpenc` -> `vah264enc` -> `x264enc` -> `jpegenc` (MJPEG), decoded on iPadOS via `AVSampleBufferDisplayLayer` (`AVFoundation` / `CoreMedia`).
- **KDE Portal Session Persistence & Scoped Input**
  - Uses `org.freedesktop.portal.RemoteDesktop` + `ScreenCast` with `persist_mode=2` (`restore_token`) so you do not have to re-approve the virtual/shared display every session. Supports switching monitors via `--reset-portal` or the HUD button in the iPad app.
  - Translates touch, right-click, and two-finger scroll directly through the portal session, with `/dev/uinput` fallback for compositors without `RemoteDesktop`.
- **iPad Touch & Desktop Gestures**
  - **1-finger tap / drag**: Left click & pointer movement with aspect-compensated coordinate mapping (`Ajustar` / `Llenar`).
  - **1-finger long press (`0.45s`)**: Right-click with haptic feedback.
  - **2-finger drag**: Smooth scrolling (`NotifyPointerAxis`).
  - **Double-tap (1 finger)** or **1-tap (2 fingers)**: Toggle top HUD overlay.

---

## Repository Structure

```text
iPadSecondMonitor/
├── .gitignore
├── LICENSE
├── README.md
├── assets/
│   └── logo.svg
├── iPadMonitor/
│   ├── Icon.png
│   ├── Info.plist
│   ├── Package.swift
│   ├── xtool.yml
│   └── Sources/iPadMonitor/
│       ├── ContentView.swift
│       ├── H264Decoder.swift
│       ├── MonitorClient.swift
│       ├── MonitorProtocol.swift
│       ├── StreamSurfaceView.swift
│       └── iPadMonitorApp.swift
├── linux-server/
│   ├── config_manager.py
│   ├── host_daemon.py
│   ├── input_handler.py
│   ├── ipad-monitor.service
│   ├── portal_session.py
│   ├── protocol.py
│   ├── start_service.sh
│   ├── usbmux_client.py
│   ├── video_pipeline.py
│   ├── wifi_server.py
│   └── tests/
│       └── test_daemon.py
└── scripts/
    ├── 99-ipad-monitor-uinput.rules
    ├── install_service.sh
    └── uninstall_service.sh
```

---

## Prerequisites (Linux Host)

Requires Python 3.10+, PipeWire, `xdg-desktop-portal` (e.g. `xdg-desktop-portal-kde` on Plasma), GStreamer (`pipewiresrc`, `vah264lpenc`/`vah264enc`/`x264enc`), PyGObject, `dbus-python`, `usbmuxd`, and `avahi` (`avahi-publish-service`) for Wi-Fi auto-discovery.

### Arch Linux / CachyOS / EndeavourOS
```bash
sudo pacman -S --needed \
  python python-gobject python-dbus \
  gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad gst-plugins-ugly gst-plugin-va \
  pipewire xdg-desktop-portal usbmuxd avahi libnotify
```

### Ubuntu / Debian
```bash
sudo apt install --no-install-recommends \
  python3 python3-gi python3-dbus gir1.2-gstreamer-1.0 \
  gstreamer1.0-pipewire gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-ugly gstreamer1.0-vaapi \
  xdg-desktop-portal usbmuxd avahi-utils libnotify-bin
```

### Fedora
```bash
sudo dnf install \
  python3 python3-gobject python3-dbus \
  gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-ugly gstreamer1-vaapi \
  pipewire-gstreamer xdg-desktop-portal usbmuxd avahi-tools libnotify
```

Enable system daemons if they are not already active:
```bash
sudo systemctl enable --now avahi-daemon usbmuxd
```

---

## Linux Service Installation

Run the portable user-service installer from the repository root:

```bash
./scripts/install_service.sh
```

This generates `~/.config/systemd/user/ipad-monitor.service` pointing to your repository path, imports `WAYLAND_DISPLAY`, `DISPLAY`, and `XDG_CURRENT_DESKTOP` into the user systemd environment, enables `ipad-monitor.service`, and prints your 4-digit Wi-Fi pairing PIN.

### Firewall (UFW) for Wi-Fi Mode

If UFW is enabled on your Linux machine, allow incoming LAN connections on TCP port `8889`:

```bash
sudo ufw allow 8889/tcp
```

### Optional `/dev/uinput` Permission Rule

Only needed if your compositor does not support `org.freedesktop.portal.RemoteDesktop`:

```bash
sudo cp scripts/99-ipad-monitor-uinput.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger /dev/uinput
```

### Uninstall Service

```bash
./scripts/uninstall_service.sh
```

---

## Host CLI & Configuration

Configuration is stored in `~/.config/ipad-monitor/config.json` (with restricted `0600` permissions).

```bash
# Run manually in foreground
./linux-server/start_service.sh

# Show current Wi-Fi PIN, local IPs, and port
python3 linux-server/host_daemon.py --show-pin

# Set a custom PIN (minimum 4 characters)
python3 linux-server/host_daemon.py --set-pin 4829

# Clear saved KDE ScreenCast/RemoteDesktop restore token (choose monitor again)
python3 linux-server/host_daemon.py --reset-portal

# Restrict mode or override ports
python3 linux-server/host_daemon.py --mode wifi --wifi-port 8889 --no-mdns
```

Supported CLI flags:
- `--mode {auto,usb,wifi}` (default from `config.json`: `auto`)
- `--usb-port PORT` (default `8888`)
- `--wifi-port PORT` (default `8889`)
- `--no-mdns`
- `--show-pin`
- `--set-pin PIN`
- `--reset-portal`

---

## Building & Running the iPadOS App (`xtool`)

The iOS/iPadOS client targets iOS 17.0+ and is configured for [xtool](https://xtool.sh/) on Linux.

1. Edit [`iPadMonitor/xtool.yml`](iPadMonitor/xtool.yml) if you want a unique `bundleID` for your own Apple Developer signing team:
   ```yaml
   version: 1
   bundleID: com.SmokePythonCL.iPadMonitor
   infoPath: Info.plist
   iconPath: Icon.png
   ```
2. Connect your iPad over USB and build/deploy:
   ```bash
   cd iPadMonitor
   xtool dev run
   ```
3. On first Wi-Fi connection, allow **Local Network** access when prompted by iPadOS (configured in [`iPadMonitor/Info.plist`](iPadMonitor/Info.plist) with `_ipadmonitor._tcp`) and enter the 4-digit PIN displayed by your Linux host (`--show-pin` or desktop notification).

---

## Running Tests

Verify the Python server modules, protocol framing, `usbmuxd`/`inotify` helpers, lazy imports, and GStreamer pipeline locally:

```bash
python3 -m py_compile linux-server/*.py
python3 -m unittest discover -s linux-server/tests -v
```

---

## License

Released under the [MIT License](LICENSE) — Copyright (c) 2026 SmokePythonCL.
