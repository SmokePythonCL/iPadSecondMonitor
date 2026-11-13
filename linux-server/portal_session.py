"""XDG Desktop Portal manager for KDE Plasma 6 (RemoteDesktop + ScreenCast with persist_mode)."""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable

from config_manager import get_restore_token, set_restore_token

_glib_loop_started = False
_glib_loop_lock = threading.Lock()


def ensure_glib_loop() -> None:
    """Lazily start DBusGMainLoop and GLib.MainLoop only when streaming starts."""
    global _glib_loop_started
    with _glib_loop_lock:
        if _glib_loop_started:
            return
        from dbus.mainloop.glib import DBusGMainLoop
        from gi.repository import GLib

        DBusGMainLoop(set_as_default=True)
        loop = GLib.MainLoop()
        threading.Thread(target=loop.run, name="glib-dbus-loop", daemon=True).start()
        _glib_loop_started = True


@dataclass
class PortalStreamInfo:
    """Resolved PipeWire stream and input capabilities returned by XDG Portal."""

    session_handle: str
    pipewire_fd: int
    node_id: int
    pw_serial: int | None
    logical_width: int
    logical_height: int
    granted_devices: int
    has_remote_desktop: bool
    rd_iface: Any | None


class PortalManager:
    """Manages XDG ScreenCast + RemoteDesktop portal sessions on KDE Plasma 6."""

    PORTAL_BUS = "org.freedesktop.portal.Desktop"
    PORTAL_PATH = "/org/freedesktop/portal/desktop"
    REQUEST_IFACE = "org.freedesktop.portal.Request"
    SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
    REMOTEDESKTOP_IFACE = "org.freedesktop.portal.RemoteDesktop"

    def __init__(self) -> None:
        self.bus = None
        self.sender_name = ""
        self.request_counter = 0
        self.session_handle: str | None = None
        self._rd_iface = None
        self._sc_iface = None
        self._available_source_types = 1 | 4
        self._used_restore_token = False
        self._use_remote_desktop = False
        self._on_ready: Callable[[PortalStreamInfo], None] | None = None
        self._on_cancel: Callable[[str], None] | None = None
        self._fallback_size = (1920, 1440)

    def _ensure_dbus(self) -> None:
        ensure_glib_loop()
        if self.bus is None:
            import dbus

            self.bus = dbus.SessionBus()
            self.sender_name = re.sub(r"\.", "_", self.bus.get_unique_name()[1:])
            portal_obj = self.bus.get_object(self.PORTAL_BUS, self.PORTAL_PATH)
            self._sc_iface = dbus.Interface(portal_obj, self.SCREENCAST_IFACE)
            try:
                props_iface = dbus.Interface(portal_obj, "org.freedesktop.DBus.Properties")
                self._available_source_types = int(
                    props_iface.Get(self.SCREENCAST_IFACE, "AvailableSourceTypes")
                )
            except Exception:
                self._available_source_types = 1 | 4
            self._rd_iface = None

    def _next_request(self) -> tuple[str, str]:
        self.request_counter += 1
        token = f"ipadmon_{os.getpid()}_{self.request_counter}"
        path = f"/org/freedesktop/portal/desktop/request/{self.sender_name}/{token}"
        return path, token

    def _listen_once(self, req_path: str, callback: Callable[[int, dict], None]) -> None:
        assert self.bus is not None
        match_holder: list[Any] = []

        def _wrapper(response_code: int, results: dict) -> None:
            if match_holder:
                try:
                    match_holder[0].remove()
                except Exception:
                    pass
            callback(int(response_code), dict(results))

        match = self.bus.add_signal_receiver(
            _wrapper,
            signal_name="Response",
            dbus_interface=self.REQUEST_IFACE,
            bus_name=self.PORTAL_BUS,
            path=req_path,
        )
        match_holder.append(match)

    def start_session(
        self,
        fallback_size: tuple[int, int],
        on_ready: Callable[[PortalStreamInfo], None],
        on_cancel: Callable[[str], None],
    ) -> None:
        """Start portal flow, trying RemoteDesktop+ScreenCast first and falling back cleanly."""
        self.close_session()
        self._fallback_size = fallback_size
        self._on_ready = on_ready
        self._on_cancel = on_cancel

        try:
            self._ensure_dbus()
        except Exception as exc:
            on_cancel(f"Error D-Bus/Portal: {exc}")
            return

        # Always start with ScreenCast so KDE opens ScreenChooserDialog for an extended Virtual screen
        # (KDE's RemoteDesktopDialog has no Virtual Output option and mirrors the primary monitor).
        self._start_flow(use_rd=False)

    def _start_flow(self, use_rd: bool = False) -> None:
        import dbus

        self._use_remote_desktop = use_rd
        self._used_restore_token = False
        req_path, req_token = self._next_request()
        self._listen_once(req_path, self._on_create_session)
        opts = {"handle_token": req_token, "session_handle_token": req_token}

        try:
            print("[Portal] Creando sesión ScreenCast para pantalla virtual extendida...")
            assert self._sc_iface is not None
            self._sc_iface.CreateSession(opts)
        except dbus.DBusException as exc:
            if self._on_cancel:
                self._on_cancel(f"Error creando sesión Portal: {exc}")

    def _on_create_session(self, code: int, results: dict) -> None:
        import dbus

        if code != 0:
            if self._use_remote_desktop:
                self._start_flow(use_rd=False)
                return
            if self._on_cancel:
                self._on_cancel("Error al crear sesión en KDE Portal.")
            return

        self.session_handle = str(results.get("session_handle", ""))
        restore_tok = get_restore_token()

        if self._use_remote_desktop and self._rd_iface is not None:
            req_path, req_token = self._next_request()
            self._listen_once(req_path, self._on_select_devices)
            dev_opts: dict[str, Any] = {
                "types": dbus.UInt32(2 | 4),  # POINTER | TOUCHSCREEN
                "persist_mode": dbus.UInt32(2),
                "handle_token": req_token,
            }
            if restore_tok:
                dev_opts["restore_token"] = dbus.String(restore_tok)
            try:
                self._rd_iface.SelectDevices(self.session_handle, dev_opts)
            except dbus.DBusException as exc:
                print(f"[Portal] SelectDevices falló ({exc}); cambiando a ScreenCast solo.")
                self.close_session()
                self._start_flow(use_rd=False)
        else:
            self._select_screencast_sources(allow_persist=True, restore_tok=restore_tok)

    def _on_select_devices(self, code: int, _results: dict) -> None:
        if code != 0:
            self.close_session()
            self._start_flow(use_rd=False)
            return
        # On RemoteDesktop sessions, persist_mode/restore_token belong strictly to SelectDevices
        self._select_screencast_sources(allow_persist=False, restore_tok=None)

    def _select_screencast_sources(self, allow_persist: bool, restore_tok: str | None) -> None:
        import dbus

        assert self._sc_iface is not None and self.session_handle is not None
        req_path, req_token = self._next_request()
        self._listen_once(req_path, self._on_select_sources)

        # Prefer VIRTUAL (4) exclusively so KDE opens directly on virtual extended screen creation
        # instead of defaulting/duplicating the physical laptop monitor.
        preferred_type = 4 if (self._available_source_types & 4) else 1
        opts: dict[str, Any] = {
            "types": dbus.UInt32(preferred_type),
            "multiple": dbus.Boolean(False),
            "cursor_mode": dbus.UInt32(2),  # Embedded cursor
            "handle_token": req_token,
        }
        if allow_persist:
            opts["persist_mode"] = dbus.UInt32(2)
            if restore_tok:
                opts["restore_token"] = dbus.String(restore_tok)
                self._used_restore_token = True

        try:
            self._sc_iface.SelectSources(self.session_handle, opts)
        except dbus.DBusException as exc:
            if preferred_type == 4 and ("types" in str(exc).lower() or "invalid" in str(exc).lower()):
                opts["types"] = dbus.UInt32(1)
                self._sc_iface.SelectSources(self.session_handle, opts)
            else:
                if self._on_cancel:
                    self._on_cancel(f"Error SelectSources: {exc}")

    def _on_select_sources(self, code: int, _results: dict) -> None:
        import dbus

        if code != 0:
            if self._on_cancel:
                self._on_cancel("Selección de fuentes cancelada.")
            return

        print("[Portal] 🔔 Solicitando inicio (se restaurará automáticamente si hay token o aparecerá diálogo KDE)...")
        req_path, req_token = self._next_request()
        self._listen_once(req_path, self._on_start_done)
        opts = {"handle_token": req_token}

        assert self.session_handle is not None
        if self._use_remote_desktop and self._rd_iface is not None:
            self._rd_iface.Start(self.session_handle, "", opts)
        else:
            assert self._sc_iface is not None
            self._sc_iface.Start(self.session_handle, "", opts)

    def _on_start_done(self, code: int, results: dict) -> None:
        import dbus

        if code != 0:
            if self._used_restore_token:
                print("[Portal] Token guardado expirado/inválido; reintentando con diálogo KDE…")
                set_restore_token(None)
                self.close_session()
                self._start_flow(use_rd=False)
                return
            print("[Portal] Diálogo cancelado o rechazado.")
            if self._on_cancel:
                self._on_cancel("Selección cancelada en el PC.")
            return

        new_token = results.get("restore_token")
        if new_token:
            set_restore_token(str(new_token))

        streams = results.get("streams", [])
        if not streams:
            if self._on_cancel:
                self._on_cancel("No se seleccionó ninguna pantalla en el PC.")
            return

        node_id = int(streams[0][0])
        props = dict(streams[0][1]) if len(streams[0]) > 1 else {}
        size_tuple = props.get("size")
        if size_tuple and len(size_tuple) == 2 and int(size_tuple[0]) > 0 and int(size_tuple[1]) > 0:
            logical_w, logical_h = int(size_tuple[0]), int(size_tuple[1])
        else:
            logical_w, logical_h = self._fallback_size

        pw_serial_raw = props.get("pipewire-serial")
        pw_serial = int(pw_serial_raw) if pw_serial_raw is not None else None
        granted_devices = int(results.get("devices", 0))

        try:
            assert self._sc_iface is not None and self.session_handle is not None
            fd_obj = self._sc_iface.OpenPipeWireRemote(
                self.session_handle,
                dbus.Dictionary(signature="sv"),
                dbus_interface=self.SCREENCAST_IFACE,
            )
            pw_fd = int(fd_obj.take())
        except Exception as exc:
            if self._on_cancel:
                self._on_cancel(f"Error abriendo PipeWire remote: {exc}")
            return

        info = PortalStreamInfo(
            session_handle=self.session_handle,
            pipewire_fd=pw_fd,
            node_id=node_id,
            pw_serial=pw_serial,
            logical_width=logical_w,
            logical_height=logical_h,
            granted_devices=granted_devices,
            has_remote_desktop=bool(self._use_remote_desktop and granted_devices != 0),
            rd_iface=self._rd_iface if self._use_remote_desktop else None,
        )
        print(
            f"[Portal] ✅ Stream activo (Node={node_id}, Serial={pw_serial}, "
            f"Size={logical_w}x{logical_h}, RemoteInput={'Portal' if info.has_remote_desktop else 'uinput'})"
        )
        if self._on_ready:
            self._on_ready(info)

    def close_session(self) -> None:
        """Close active XDG Portal session if open."""
        if self.session_handle and self.bus is not None:
            try:
                import dbus

                obj = self.bus.get_object(self.PORTAL_BUS, self.session_handle)
                dbus.Interface(obj, "org.freedesktop.portal.Session").Close()
            except Exception:
                pass
        self.session_handle = None
