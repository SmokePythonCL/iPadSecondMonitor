"""Event-driven usbmuxd client (pure Python AF_UNIX + inotify, zero iproxy/pkill)."""

from __future__ import annotations

import ctypes
import os
import plistlib
import select
import socket
import struct
import threading
import time
from typing import Callable

from protocol import recv_exact

USBMUXD_PATH = "/var/run/usbmuxd"
MSG_PLIST = 8
PROTO_VERSION = 1

# Linux inotify constants (verified against sys/inotify.h & bits/inotify.h)
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_NONBLOCK = 0x00000800
IN_CLOEXEC = 0x00080000
INOTIFY_EVENT_FMT = "<iIII"
INOTIFY_EVENT_SIZE = struct.calcsize(INOTIFY_EVENT_FMT)


def pack_mux_plist(payload: dict, tag: int = 1) -> bytes:
    """Serialize a plist message with the 16-byte usbmuxd v1 header."""
    body = plistlib.dumps(payload, fmt=plistlib.FMT_XML)
    header = struct.pack("<IIII", 16 + len(body), PROTO_VERSION, MSG_PLIST, tag)
    return header + body


def read_mux_plist(sock: socket.socket) -> tuple[int, dict]:
    """Read and parse one usbmuxd v1 plist message from a connected socket."""
    header = recv_exact(sock, 16)
    total_len, _version, _msg_type, tag = struct.unpack("<IIII", header)
    body_len = total_len - 16
    if body_len <= 0 or body_len > 1024 * 1024:
        raise ValueError(f"Tamaño de mensaje usbmuxd inválido: {body_len}")
    body = recv_exact(sock, body_len)
    data = plistlib.loads(body)
    if not isinstance(data, dict):
        raise ValueError("Payload usbmuxd no es diccionario plist")
    return tag, data


def connect_usb_port(device_id: int, port: int, timeout: float = 5.0) -> socket.socket | None:
    """Open a direct TCP-over-USB mux tunnel to device port via /var/run/usbmuxd."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(USBMUXD_PATH)
        req = {
            "MessageType": "Connect",
            "ClientVersionString": "iPadSecondMonitor",
            "ProgName": "host_daemon",
            "DeviceID": device_id,
            "PortNumber": socket.htons(port),
        }
        sock.sendall(pack_mux_plist(req, tag=1))
        _, resp = read_mux_plist(sock)
        if resp.get("MessageType") == "Result" and resp.get("Number") == 0:
            sock.settimeout(None)
            return sock
        sock.close()
        return None
    except OSError:
        sock.close()
        return None


def parse_inotify_names(buf: bytes) -> list[str]:
    """Extract null-stripped filenames from a buffer of Linux inotify_event structs."""
    names: list[str] = []
    offset = 0
    while offset + INOTIFY_EVENT_SIZE <= len(buf):
        _wd, _mask, _cookie, name_len = struct.unpack_from(INOTIFY_EVENT_FMT, buf, offset)
        offset += INOTIFY_EVENT_SIZE
        if name_len > 0 and offset + name_len <= len(buf):
            raw_name = buf[offset : offset + name_len]
            names.append(raw_name.split(b"\x00", 1)[0].decode("utf-8", errors="ignore"))
        offset += name_len
    return names


class _InotifySocketDirWatcher:
    """Zero-CPU kernel watcher waiting for /run/usbmuxd creation."""

    def __init__(self, target_path: str = USBMUXD_PATH) -> None:
        real_target = os.path.realpath(target_path)
        self.watch_dir = os.path.dirname(real_target)
        self.target_name = os.path.basename(real_target)
        self._libc = ctypes.CDLL("libc.so.6", use_errno=True)

    def wait_until_exists(self, stop_fd: int) -> bool:
        """Block passively until target socket exists or stop_fd becomes readable."""
        if os.path.exists(USBMUXD_PATH):
            return True

        fd = self._libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if fd < 0:
            # Fallback gentle wait only if inotify unavailable in restricted environment
            while not os.path.exists(USBMUXD_PATH):
                r, _, _ = select.select([stop_fd], [], [], 2.0)
                if r:
                    return False
            return True

        try:
            wd = self._libc.inotify_add_watch(
                fd,
                self.watch_dir.encode("utf-8"),
                IN_CREATE | IN_MOVED_TO,
            )
            if wd < 0:
                return False

            if os.path.exists(USBMUXD_PATH):
                return True

            while True:
                readable, _, _ = select.select([fd, stop_fd], [], [])
                if stop_fd in readable:
                    return False
                if fd in readable:
                    try:
                        buf = os.read(fd, 4096)
                    except BlockingIOError:
                        continue
                    if self.target_name in parse_inotify_names(buf) or os.path.exists(USBMUXD_PATH):
                        return True
        finally:
            os.close(fd)


class UsbmuxWatcher:
    """Watches USB iOS devices via usbmuxd and opens tunnels when ready."""

    def __init__(
        self,
        usb_port: int,
        on_connected: Callable[[socket.socket, str], None],
        is_busy: Callable[[], bool],
    ) -> None:
        self.usb_port = usb_port
        self.on_connected = on_connected
        self.is_busy = is_busy

        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._stop_r, self._stop_w = os.pipe()
        self._devices_lock = threading.Lock()
        self._devices: dict[int, dict] = {}
        self._dir_watcher = _InotifySocketDirWatcher(USBMUXD_PATH)

    def start(self) -> None:
        """Start event-driven USB watcher and connector threads."""
        threading.Thread(target=self._watch_loop, name="usbmux-watch", daemon=True).start()
        threading.Thread(target=self._connect_loop, name="usbmux-connect", daemon=True).start()

    def stop(self) -> None:
        """Stop threads and release pipes cleanly."""
        self._stop_event.set()
        self._wake_event.set()
        try:
            os.write(self._stop_w, b"x")
        except OSError:
            pass
        for fd in (self._stop_r, self._stop_w):
            try:
                os.close(fd)
            except OSError:
                pass

    def notify_session_ended(self) -> None:
        """Wake connector when an active streaming session finishes."""
        self._wake_event.set()

    def _watch_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._dir_watcher.wait_until_exists(self._stop_r):
                break

            # Allow usbmuxd a brief moment to bind after socket creation
            mux_sock = self._open_listen_socket()
            if not mux_sock:
                self._stop_event.wait(0.5)
                continue

            try:
                while not self._stop_event.is_set():
                    readable, _, _ = select.select([mux_sock, self._stop_r], [], [])
                    if self._stop_r in readable:
                        break
                    if mux_sock in readable:
                        _, msg = read_mux_plist(mux_sock)
                        self._handle_mux_event(msg)
            except (ConnectionError, OSError, ValueError):
                pass
            finally:
                with self._devices_lock:
                    self._devices.clear()
                try:
                    mux_sock.close()
                except OSError:
                    pass

    def _open_listen_socket(self) -> socket.socket | None:
        for _ in range(5):
            if self._stop_event.is_set():
                return None
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(USBMUXD_PATH)
                listen_req = {
                    "MessageType": "Listen",
                    "ClientVersionString": "iPadSecondMonitor",
                    "ProgName": "host_daemon",
                    "kLibUSBMuxVersion": 3,
                }
                sock.sendall(pack_mux_plist(listen_req, tag=1))
                _, resp = read_mux_plist(sock)
                if resp.get("MessageType") == "Result" and resp.get("Number") == 0:
                    return sock
                sock.close()
                return None
            except OSError:
                sock.close()
                time.sleep(0.15)
        return None

    def _handle_mux_event(self, msg: dict) -> None:
        mtype = msg.get("MessageType")
        if mtype == "Attached":
            props = msg.get("Properties") or {}
            if props.get("ConnectionType", "USB") != "USB":
                return
            dev_id = msg.get("DeviceID")
            if isinstance(dev_id, int):
                serial = str(props.get("SerialNumber", ""))[:8]
                with self._devices_lock:
                    is_new = dev_id not in self._devices
                    self._devices[dev_id] = props
                if is_new:
                    print(f"[USB] 🔌 Dispositivo detectado por usbmuxd (ID={dev_id}, S/N={serial}…).")
                self._wake_event.set()
        elif mtype == "Detached":
            dev_id = msg.get("DeviceID")
            if isinstance(dev_id, int):
                with self._devices_lock:
                    removed = self._devices.pop(dev_id, None)
                if removed is not None:
                    print(f"[USB] ⏏️ Dispositivo desconectado (ID={dev_id}).")
                self._wake_event.set()

    def _connect_loop(self) -> None:
        backoff = 2.0
        while not self._stop_event.is_set():
            with self._devices_lock:
                dev_items = list(self._devices.items())

            if not dev_items or self.is_busy():
                backoff = 2.0
                self._wake_event.wait()
                self._wake_event.clear()
                continue

            connected_any = False
            for dev_id, props in dev_items:
                if self._stop_event.is_set() or self.is_busy():
                    break
                tunnel = connect_usb_port(dev_id, self.usb_port)
                if tunnel is not None:
                    serial = str(props.get("SerialNumber", ""))[:8] or str(dev_id)
                    backoff = 2.0
                    connected_any = True
                    # Hand over tunnel synchronously until the session exits
                    self.on_connected(tunnel, f"USB ({serial})")
                    break

            if not connected_any:
                # Device is plugged in but iPad app listener isn't open yet; back off gently
                self._wake_event.wait(timeout=backoff)
                self._wake_event.clear()
                backoff = min(5.0, backoff + 1.0)
