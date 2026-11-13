"""Passive Wi-Fi TCP server, Avahi mDNS publisher, and PIN authentication."""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
from typing import Callable

from protocol import (
    OP_AUTH_RESPONSE,
    OP_CMD_ACK,
    PacketWriter,
    generate_nonce,
    recv_exact,
    verify_pin_hmac,
)


def get_local_ipv4_addresses() -> list[str]:
    """Return likely LAN IPv4 addresses for display without external traffic."""
    ips: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    if not ips:
        try:
            host_ips = socket.gethostbyname_ex(socket.gethostname())[2]
            ips = [ip for ip in host_ips if not ip.startswith("127.")]
        except OSError:
            pass
    return ips


class WifiServer:
    """Passive TCP listener on 0.0.0.0:<port> with mDNS advertisement and PIN auth."""

    def __init__(
        self,
        port: int,
        get_pin: Callable[[], str],
        on_authenticated: Callable[[socket.socket, PacketWriter, str], None],
        is_busy: Callable[[], bool],
        enable_mdns: bool = True,
    ) -> None:
        self.port = port
        self.get_pin = get_pin
        self.on_authenticated = on_authenticated
        self.is_busy = is_busy
        self.enable_mdns = enable_mdns

        self._stop_event = threading.Event()
        self._server_sock: socket.socket | None = None
        self._avahi_proc: subprocess.Popen | None = None
        self._last_notify_ts: float = 0.0

    def start(self) -> None:
        """Bind TCP socket, optionally publish mDNS service, and accept connections."""
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind(("0.0.0.0", self.port))
        self._server_sock.listen(2)
        self._server_sock.settimeout(1.0)

        if self.enable_mdns:
            self._start_mdns()

        threading.Thread(target=self._accept_loop, name="wifi-accept", daemon=True).start()

    def stop(self) -> None:
        """Stop mDNS subprocess and close server socket."""
        self._stop_event.set()
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass
            self._server_sock = None
        if self._avahi_proc:
            try:
                self._avahi_proc.terminate()
                self._avahi_proc.wait(timeout=2)
            except Exception:
                try:
                    self._avahi_proc.kill()
                    self._avahi_proc.wait()
                except Exception:
                    pass
            self._avahi_proc = None

    def _start_mdns(self) -> None:
        avahi_bin = shutil.which("avahi-publish-service")
        if not avahi_bin:
            print("[Wi-Fi] Aviso: avahi-publish-service no encontrado; conecta manualmente por IP.")
            return
        hostname = socket.gethostname() or "Linux"
        try:
            self._avahi_proc = subprocess.Popen(
                [avahi_bin, f"iPadMonitor ({hostname})", "_ipadmonitor._tcp", str(self.port), "proto=2"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print(f"[Wi-Fi] 📡 Servicio Bonjour/mDNS anunciado en puerto TCP {self.port} (_ipadmonitor._tcp).")
        except OSError as exc:
            print(f"[Wi-Fi] Aviso iniciando Avahi: {exc}")

    def _accept_loop(self) -> None:
        assert self._server_sock is not None
        while not self._stop_event.is_set():
            try:
                client_sock, addr = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            client_sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            threading.Thread(
                target=self._handle_client,
                args=(client_sock, addr),
                name=f"wifi-auth-{addr[0]}",
                daemon=True,
            ).start()

    def _notify_pin_on_desktop(self, client_ip: str, pin: str) -> None:
        now = time.monotonic()
        if now - self._last_notify_ts < 20.0:
            return
        self._last_notify_ts = now
        notify_bin = shutil.which("notify-send")
        if not notify_bin:
            return
        try:
            subprocess.Popen(
                [
                    notify_bin,
                    "-a",
                    "iPad Second Monitor",
                    "-i",
                    "video-display",
                    f"Conexión Wi-Fi desde {client_ip}",
                    f"Introduce este PIN en tu iPad: {pin}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass

    def _handle_client(self, client_sock: socket.socket, addr: tuple[str, int]) -> None:
        client_ip = addr[0]
        writer = PacketWriter(client_sock)

        if self.is_busy():
            writer.send_status("Ya hay una sesión activa en curso (USB o Wi-Fi).")
            writer.close()
            return

        print(f"[Wi-Fi] 📶 Conexión entrante desde {client_ip}, solicitando PIN...")
        pin = self.get_pin()
        self._notify_pin_on_desktop(client_ip, pin)

        try:
            client_sock.settimeout(120.0)
            nonce = generate_nonce()
            writer.send_auth_challenge(nonce)

            attempts = 0
            max_attempts = 5
            authenticated = False

            while attempts < max_attempts and not self._stop_event.is_set():
                opcode_bytes = recv_exact(client_sock, 1)
                opcode = opcode_bytes[0]

                if opcode == OP_CMD_ACK:
                    continue
                if opcode != OP_AUTH_RESPONSE:
                    # Wait only for auth response before permitting any command
                    writer.send_status("Se requiere autenticación por PIN.")
                    break

                response_hmac = recv_exact(client_sock, 32)
                pin = self.get_pin()
                if verify_pin_hmac(pin, nonce, response_hmac):
                    authenticated = True
                    break

                attempts += 1
                print(f"[Wi-Fi] ❌ PIN incorrecto desde {client_ip} (intento {attempts}/{max_attempts}).")
                writer.send_auth_result(False)
                writer.send_status("PIN incorrecto. Intenta nuevamente.")
                time.sleep(1.5)
                if attempts < max_attempts:
                    nonce = generate_nonce()
                    writer.send_auth_challenge(nonce)

            if not authenticated:
                writer.close()
                return

            client_sock.settimeout(None)
            writer.send_auth_result(True)
            print(f"[Wi-Fi] 🟢 Cliente autenticado: {client_ip}")
            self.on_authenticated(client_sock, writer, f"Wi-Fi ({client_ip})")
        except (ConnectionError, OSError, socket.timeout):
            writer.close()
