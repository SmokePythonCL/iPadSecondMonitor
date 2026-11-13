#!/usr/bin/env python3
"""Unified USB (usbmuxd) and Wi-Fi (mDNS + PIN) host daemon for iPad Second Monitor."""

from __future__ import annotations

import argparse
import os
import signal
import socket
import struct
import sys
import threading

from config_manager import load_config, set_pin, set_restore_token
from input_handler import create_input_controller
from portal_session import PortalManager, PortalStreamInfo
from protocol import (
    CODEC_AUTO,
    OP_CMD_ACK,
    OP_CMD_RESET_PORTAL,
    OP_CMD_START,
    OP_CMD_STOP,
    OP_TOUCH_EVENT,
    PacketWriter,
    recv_exact,
)
from usbmux_client import UsbmuxWatcher
from video_pipeline import VideoStreamer, apply_custom_resolution
from wifi_server import WifiServer, get_local_ipv4_addresses


class ActiveSession:
    """Manages one authenticated iPad connection (USB or Wi-Fi)."""

    def __init__(self, sock: socket.socket, writer: PacketWriter, label: str, is_wifi: bool) -> None:
        self.sock = sock
        self.writer = writer
        self.label = label
        self.is_wifi = is_wifi
        self.portal = PortalManager()
        self.streamer = VideoStreamer()
        self.input_ctrl = None
        self.target_width = 1920
        self.target_height = 1440
        self.codec_pref = CODEC_AUTO
        self._verified_logged = is_wifi

    def run(self) -> None:
        """Read commands from the connected iPad until disconnected."""
        self.writer.send_status(f"PC Conectado ({self.label}). Pulsa 'Iniciar Pantalla en Linux'.")
        try:
            while True:
                op_bytes = recv_exact(self.sock, 1)
                opcode = op_bytes[0]

                if not self._verified_logged:
                    print(f"\n[Sesión] 🟢 iPad activo por {self.label}.")
                    self._verified_logged = True

                if opcode == OP_CMD_ACK:
                    continue
                elif opcode == OP_CMD_START:
                    self._handle_start()
                elif opcode == OP_CMD_STOP:
                    print("[iPad] Comando DETENER recibido.")
                    self.stop_media()
                    self.writer.send_status("Pantalla detenida.")
                elif opcode == OP_TOUCH_EVENT:
                    data = recv_exact(self.sock, 13)
                    ev_type, nx, ny, _pressure = struct.unpack(">Bfff", data)
                    if self.input_ctrl is not None:
                        self.input_ctrl.handle_event(ev_type, nx, ny)
                elif opcode == OP_CMD_RESET_PORTAL:
                    set_restore_token(None)
                    self.writer.send_status("Selección de pantalla reiniciada. Pulsa Iniciar para elegir otra.")
                    print("[Portal] Token de pantalla olvidado por solicitud del iPad.")
        except (ConnectionError, OSError):
            pass
        finally:
            self.stop_media()
            self.writer.close()
            if self._verified_logged:
                print(f"[Sesión] Desconectado ({self.label}).")

    def _handle_start(self) -> None:
        # Payload: width (4B) + height (4B) + scale (4B) + codec_pref (1B)
        params = recv_exact(self.sock, 13)
        w, h, _scale, codec_pref = struct.unpack(">IIfB", params)
        if w > 0 and h > 0:
            self.target_width = w
            self.target_height = h
        self.codec_pref = codec_pref
        print(f"[iPad] Iniciar pantalla solicitado ({self.target_width}x{self.target_height}, codec_pref={codec_pref}).")

        self.writer.send_status("Iniciando pantalla virtual en KDE Plasma…")
        self.portal.start_session(
            fallback_size=(self.target_width, self.target_height),
            on_ready=self._on_portal_ready,
            on_cancel=self._on_portal_cancelled,
        )

    def _on_portal_ready(self, stream: PortalStreamInfo) -> None:
        virtual_output = apply_custom_resolution(self.target_width, self.target_height)
        self.input_ctrl = create_input_controller(stream, virtual_output=virtual_output)
        self.streamer.start(
            stream=stream,
            writer=self.writer,
            codec_pref=self.codec_pref,
            is_wifi=self.is_wifi,
            on_finished=self._on_stream_stopped,
        )

    def _on_portal_cancelled(self, reason: str) -> None:
        self.writer.send_status(reason)

    def _on_stream_stopped(self) -> None:
        self.stop_media()

    def stop_media(self) -> None:
        """Stop video capture, virtual input, and KDE portal session."""
        self.streamer.stop()
        if self.input_ctrl is not None:
            self.input_ctrl.close()
            self.input_ctrl = None
        self.portal.close_session()


class iPadHostDaemon:
    """Coordinates idle USB + Wi-Fi listeners with near-zero standby footprint."""

    def __init__(self, mode: str, usb_port: int, wifi_port: int, enable_mdns: bool) -> None:
        self.mode = mode
        self.usb_port = usb_port
        self.wifi_port = wifi_port
        self.enable_mdns = enable_mdns

        self._session_lock = threading.Lock()
        self._active_session: ActiveSession | None = None
        self._stop_event = threading.Event()
        self._usbmux: UsbmuxWatcher | None = None
        self._wifi: WifiServer | None = None

    def is_busy(self) -> bool:
        """Return True when an iPad session is currently connected."""
        with self._session_lock:
            return self._active_session is not None

    def _run_session(self, sock: socket.socket, writer: PacketWriter, label: str, is_wifi: bool) -> None:
        with self._session_lock:
            if self._active_session is not None:
                writer.close()
                return
            session = ActiveSession(sock, writer, label, is_wifi)
            self._active_session = session

        try:
            session.run()
        finally:
            with self._session_lock:
                self._active_session = None
            if self._usbmux is not None:
                self._usbmux.notify_session_ended()

    def _on_usb_connected(self, sock: socket.socket, label: str) -> None:
        writer = PacketWriter(sock)
        writer.send_auth_result(True)
        self._run_session(sock, writer, label, is_wifi=False)

    def _on_wifi_authenticated(self, sock: socket.socket, writer: PacketWriter, label: str) -> None:
        self._run_session(sock, writer, label, is_wifi=True)

    def run(self) -> None:
        """Start enabled listeners and block passively until termination signal."""
        cfg = load_config()

        if self.mode in ("auto", "wifi"):
            self._wifi = WifiServer(
                port=self.wifi_port,
                get_pin=lambda: load_config()["pin"],
                on_authenticated=self._on_wifi_authenticated,
                is_busy=self.is_busy,
                enable_mdns=self.enable_mdns,
            )
            self._wifi.start()
            ips = ", ".join(get_local_ipv4_addresses()) or "IP local"
            print(f"[Wi-Fi] 🔐 PIN de emparejamiento: {cfg['pin']} | Escuchando en {ips}:{self.wifi_port}")

        if self.mode in ("auto", "usb"):
            self._usbmux = UsbmuxWatcher(
                usb_port=self.usb_port,
                on_connected=self._on_usb_connected,
                is_busy=self.is_busy,
            )
            self._usbmux.start()
            print(f"[USB] 🔍 Monitor pasivo usbmuxd activo (puerto dispositivo {self.usb_port}).")

        print("[Daemon] 💤 En modo espera eficiente. Conecta por USB o Wi-Fi desde el iPad.")
        self._stop_event.wait()

    def shutdown(self) -> None:
        """Gracefully stop active session and passive listeners."""
        self._stop_event.set()
        with self._session_lock:
            if self._active_session is not None:
                self._active_session.writer.close()
        if self._wifi is not None:
            self._wifi.stop()
        if self._usbmux is not None:
            self._usbmux.stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="iPad Second Monitor - Host Daemon (USB + Wi-Fi)")
    parser.add_argument("--mode", choices=("auto", "usb", "wifi"), help="Modo de conexión (por defecto config: auto)")
    parser.add_argument("--usb-port", type=int, help="Puerto TCP en iPad para USB usbmuxd (por defecto 8888)")
    parser.add_argument("--wifi-port", type=int, help="Puerto TCP de escucha Wi-Fi en Linux (por defecto 8889)")
    parser.add_argument("--no-mdns", action="store_true", help="Desactiva anuncio Avahi/Bonjour mDNS")
    parser.add_argument("--show-pin", action="store_true", help="Muestra el PIN actual e IPs Wi-Fi y sale")
    parser.add_argument("--set-pin", type=str, help="Define un nuevo PIN Wi-Fi (mínimo 4 caracteres) y sale")
    parser.add_argument("--reset-portal", action="store_true", help="Borra el token guardado de pantalla KDE y sale")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config()

    if args.set_pin is not None:
        updated = set_pin(args.set_pin)
        print(f"PIN actualizado correctamente: {updated['pin']}")
        return

    if args.reset_portal:
        set_restore_token(None)
        print("Token de pantalla virtual de KDE reiniciado.")
        return

    if args.show_pin:
        ips = ", ".join(get_local_ipv4_addresses()) or "No detectada"
        wifi_port = args.wifi_port or int(cfg["wifi_port"])
        print(f"PIN Wi-Fi actual : {cfg['pin']}")
        print(f"IPs locales      : {ips}")
        print(f"Puerto Wi-Fi TCP : {wifi_port}")
        return

    mode = args.mode or str(cfg.get("mode", "auto"))
    usb_port = args.usb_port or int(cfg.get("usb_port", 8888))
    wifi_port = args.wifi_port or int(cfg.get("wifi_port", 8889))

    print("=" * 60)
    print(f"  iPad Second Monitor Daemon (Modo: {mode.upper()}, PID: {os.getpid()})")
    print("=" * 60)

    daemon = iPadHostDaemon(
        mode=mode,
        usb_port=usb_port,
        wifi_port=wifi_port,
        enable_mdns=not args.no_mdns,
    )

    def _on_signal(_signum: int, _frame: object) -> None:
        daemon.shutdown()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    daemon.run()


if __name__ == "__main__":
    main()
