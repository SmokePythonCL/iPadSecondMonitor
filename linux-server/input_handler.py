"""Hybrid input controller: XDG RemoteDesktop Portal (scoped to virtual screen) + /dev/uinput fallback."""

from __future__ import annotations

import fcntl
import os
import struct
import threading
import time
from typing import Any

from portal_session import PortalStreamInfo
from protocol import TOUCH_DOWN, TOUCH_MOVE, TOUCH_RIGHT_CLICK, TOUCH_SCROLL, TOUCH_UP

BTN_LEFT_CODE = 272   # 0x110
BTN_RIGHT_CODE = 273  # 0x111


def _bind_kwin_output_async(device_name: str, output_name: str) -> None:
    """Bind the created uinput device specifically to the extended Virtual output in KWin."""

    def _worker() -> None:
        try:
            import dbus

            bus = dbus.SessionBus()
            mgr_obj = bus.get_object("org.kde.KWin", "/org/kde/KWin/InputDevice")
            mgr_props = dbus.Interface(mgr_obj, "org.freedesktop.DBus.Properties")

            for _ in range(12):
                sys_names = mgr_props.Get("org.kde.KWin.InputDeviceManager", "devicesSysNames")
                for dev_sys in sys_names:
                    dev_obj = bus.get_object("org.kde.KWin", f"/org/kde/KWin/InputDevice/{dev_sys}")
                    dev_props = dbus.Interface(dev_obj, "org.freedesktop.DBus.Properties")
                    if str(dev_props.Get("org.kde.KWin.InputDevice", "name")) == device_name:
                        try:
                            dev_props.Set("org.kde.KWin.InputDevice", "mapToWorkspace", dbus.Boolean(False))
                        except Exception:
                            pass
                        dev_props.Set("org.kde.KWin.InputDevice", "outputName", dbus.String(output_name))
                        print(f"[Input] 🎯 Táctil vinculado exclusivamente a {output_name} ({dev_sys}).")
                        return
                time.sleep(0.08)
        except Exception as exc:
            print(f"[Input] Aviso vinculando KWin outputName: {exc}")

    threading.Thread(target=_worker, name="kwin-input-bind", daemon=True).start()


class UinputTouchDevice:
    """Kernel virtual touch/pointer fallback via /dev/uinput."""

    DEVICE_NAME = "iPad Virtual Touchscreen"
    SCROLL_DEVICE_NAME = "iPad Virtual Scroll"

    UI_SET_EVBIT = 0x40045564
    UI_SET_KEYBIT = 0x40045565
    UI_SET_RELBIT = 0x40045566
    UI_SET_ABSBIT = 0x40045567
    UI_DEV_CREATE = 0x5501
    UI_DEV_DESTROY = 0x5502

    EV_SYN = 0x00
    EV_KEY = 0x01
    EV_REL = 0x02
    EV_ABS = 0x03
    SYN_REPORT = 0x00
    BTN_TOUCH = 0x14A
    BTN_LEFT = 0x110
    BTN_RIGHT = 0x111
    REL_X = 0x00
    REL_Y = 0x01
    REL_HWHEEL = 0x06
    REL_WHEEL = 0x08
    REL_WHEEL_HI_RES = 0x0B
    REL_HWHEEL_HI_RES = 0x0C
    ABS_X = 0x00
    ABS_Y = 0x01

    def __init__(self, width: int = 1920, height: int = 1080, virtual_output: str | None = None) -> None:
        self.width = max(1, width)
        self.height = max(1, height)
        self.virtual_output = virtual_output
        self.fd: int | None = None
        self._scroll_fd: int | None = None
        self._hires_acc_x = 0.0
        self._hires_acc_y = 0.0
        self._open_device()

    def _open_device(self) -> None:
        try:
            self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
            fcntl.ioctl(self.fd, self.UI_SET_EVBIT, self.EV_KEY)
            fcntl.ioctl(self.fd, self.UI_SET_KEYBIT, self.BTN_TOUCH)
            fcntl.ioctl(self.fd, self.UI_SET_KEYBIT, self.BTN_LEFT)
            fcntl.ioctl(self.fd, self.UI_SET_KEYBIT, self.BTN_RIGHT)
            fcntl.ioctl(self.fd, self.UI_SET_EVBIT, self.EV_ABS)
            fcntl.ioctl(self.fd, self.UI_SET_ABSBIT, self.ABS_X)
            fcntl.ioctl(self.fd, self.UI_SET_ABSBIT, self.ABS_Y)

            absmin = [0] * 64
            absmax = [0] * 64
            absmax[self.ABS_X] = self.width
            absmax[self.ABS_Y] = self.height

            name = self.DEVICE_NAME.encode("ascii").ljust(80, b"\x00")
            bus_type, vendor, product, version = 0x03, 0x05AC, 0x0001, 1
            uinput_dev = struct.pack("80sHHHHI", name, bus_type, vendor, product, version, 0)
            uinput_dev += struct.pack("64i", *absmax)
            uinput_dev += struct.pack("64i", *absmin)
            uinput_dev += struct.pack("64i", *([0] * 64))
            uinput_dev += struct.pack("64i", *([0] * 64))

            os.write(self.fd, uinput_dev)
            fcntl.ioctl(self.fd, self.UI_DEV_CREATE)
            print("[Input] ✅ Dispositivo táctil /dev/uinput activo.")
            if self.virtual_output:
                _bind_kwin_output_async(self.DEVICE_NAME, self.virtual_output)
        except Exception as exc:
            print(f"[Input] Aviso: /dev/uinput no disponible ({exc}).")
            self.fd = None
            return

        # Open a companion pure relative mouse for clean 2-finger smooth scrolling
        try:
            sfd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
            fcntl.ioctl(sfd, self.UI_SET_EVBIT, self.EV_KEY)
            fcntl.ioctl(sfd, self.UI_SET_KEYBIT, self.BTN_LEFT)
            fcntl.ioctl(sfd, self.UI_SET_EVBIT, self.EV_REL)
            for code in (
                self.REL_X,
                self.REL_Y,
                self.REL_WHEEL,
                self.REL_HWHEEL,
                self.REL_WHEEL_HI_RES,
                self.REL_HWHEEL_HI_RES,
            ):
                fcntl.ioctl(sfd, self.UI_SET_RELBIT, code)

            s_name = self.SCROLL_DEVICE_NAME.encode("ascii").ljust(80, b"\x00")
            s_dev = struct.pack("80sHHHHI", s_name, 0x03, 0x05AC, 0x0002, 1, 0)
            s_dev += struct.pack("64i", *([0] * 64)) * 4
            os.write(sfd, s_dev)
            fcntl.ioctl(sfd, self.UI_DEV_CREATE)
            self._scroll_fd = sfd
        except Exception:
            self._scroll_fd = None

    def _emit_to(self, target_fd: int | None, type_code: int, code: int, value: int) -> None:
        if target_fd is None:
            return
        now = time.time()
        sec = int(now)
        usec = int((now - sec) * 1_000_000)
        event = struct.pack("qqHHi", sec, usec, type_code, code, value)
        try:
            os.write(target_fd, event)
        except OSError:
            pass

    def _emit(self, type_code: int, code: int, value: int) -> None:
        self._emit_to(self.fd, type_code, code, value)

    def _syn(self, target_fd: int | None = None) -> None:
        self._emit_to(self.fd if target_fd is None else target_fd, self.EV_SYN, self.SYN_REPORT, 0)

    def _coords(self, nx: float, ny: float) -> tuple[int, int]:
        x = max(0, min(self.width, int(nx * self.width)))
        y = max(0, min(self.height, int(ny * self.height)))
        return x, y

    def _emit_scroll(self, dx: float, dy: float) -> None:
        if self._scroll_fd is None:
            return
        # StreamSurfaceView sends negative delta*0.15; Linux REL_WHEEL uses positive=up, negative=down
        v_hires = int(round(-dy * 40.0))
        h_hires = int(round(-dx * 40.0))
        if v_hires != 0:
            self._emit_to(self._scroll_fd, self.EV_REL, self.REL_WHEEL_HI_RES, v_hires)
            self._hires_acc_y += v_hires
            notches_y = int(self._hires_acc_y / 120.0)
            if notches_y != 0:
                self._emit_to(self._scroll_fd, self.EV_REL, self.REL_WHEEL, notches_y)
                self._hires_acc_y -= notches_y * 120.0
        if h_hires != 0:
            self._emit_to(self._scroll_fd, self.EV_REL, self.REL_HWHEEL_HI_RES, h_hires)
            self._hires_acc_x += h_hires
            notches_x = int(self._hires_acc_x / 120.0)
            if notches_x != 0:
                self._emit_to(self._scroll_fd, self.EV_REL, self.REL_HWHEEL, notches_x)
                self._hires_acc_x -= notches_x * 120.0
        if v_hires != 0 or h_hires != 0:
            self._syn(self._scroll_fd)

    def handle_event(self, ev_type: int, nx: float, ny: float) -> None:
        """Process touch, right-click, and smooth scroll events using uinput."""
        if self.fd is None:
            return
        if ev_type == TOUCH_DOWN:
            x, y = self._coords(nx, ny)
            self._emit(self.EV_ABS, self.ABS_X, x)
            self._emit(self.EV_ABS, self.ABS_Y, y)
            self._emit(self.EV_KEY, self.BTN_TOUCH, 1)
            self._emit(self.EV_KEY, self.BTN_LEFT, 1)
            self._syn()
        elif ev_type == TOUCH_MOVE:
            x, y = self._coords(nx, ny)
            self._emit(self.EV_ABS, self.ABS_X, x)
            self._emit(self.EV_ABS, self.ABS_Y, y)
            self._syn()
        elif ev_type == TOUCH_UP:
            self._emit(self.EV_KEY, self.BTN_TOUCH, 0)
            self._emit(self.EV_KEY, self.BTN_LEFT, 0)
            self._syn()
        elif ev_type == TOUCH_RIGHT_CLICK:
            x, y = self._coords(nx, ny)
            self._emit(self.EV_ABS, self.ABS_X, x)
            self._emit(self.EV_ABS, self.ABS_Y, y)
            self._emit(self.EV_KEY, self.BTN_RIGHT, 1)
            self._syn()
            self._emit(self.EV_KEY, self.BTN_RIGHT, 0)
            self._syn()
        elif ev_type == TOUCH_SCROLL:
            self._emit_scroll(nx, ny)

    def close(self) -> None:
        """Destroy virtual uinput devices."""
        for attr in ("fd", "_scroll_fd"):
            handle = getattr(self, attr)
            if handle is not None:
                try:
                    fcntl.ioctl(handle, self.UI_DEV_DESTROY)
                    os.close(handle)
                except OSError:
                    pass
                setattr(self, attr, None)


class PortalRemoteInput:
    """Scoped pointer/touch injection via org.freedesktop.portal.RemoteDesktop."""

    def __init__(self, stream: PortalStreamInfo) -> None:
        self.stream = stream
        self._rd: Any = stream.rd_iface
        self._has_pointer = bool(stream.granted_devices & 2)
        self._has_touch = bool(stream.granted_devices & 4)
        self._touch_active = False
        print(
            f"[Input] ✅ Control remoto Portal activo para pantalla {stream.logical_width}x{stream.logical_height} "
            f"(pointer={self._has_pointer}, touch={self._has_touch})."
        )

    def _abs_xy(self, nx: float, ny: float) -> tuple[float, float]:
        x = max(0.0, min(float(self.stream.logical_width), nx * self.stream.logical_width))
        y = max(0.0, min(float(self.stream.logical_height), ny * self.stream.logical_height))
        return x, y

    def handle_event(self, ev_type: int, nx: float, ny: float) -> None:
        """Forward normalized iPad touch/gesture into the exact virtual monitor stream."""
        import dbus

        empty = dbus.Dictionary(signature="sv")
        session = self.stream.session_handle
        node = dbus.UInt32(self.stream.node_id)

        try:
            if self._has_pointer:
                self._handle_as_pointer(ev_type, nx, ny, session, node, empty)
            elif self._has_touch:
                self._handle_as_touch(ev_type, nx, ny, session, node, empty)
        except Exception:
            pass

    def _handle_as_pointer(
        self, ev_type: int, nx: float, ny: float, session: str, node: Any, empty: Any
    ) -> None:
        import dbus

        if ev_type == TOUCH_DOWN:
            x, y = self._abs_xy(nx, ny)
            self._rd.NotifyPointerMotionAbsolute(session, empty, node, dbus.Double(x), dbus.Double(y))
            self._rd.NotifyPointerButton(session, empty, dbus.Int32(BTN_LEFT_CODE), dbus.UInt32(1))
        elif ev_type == TOUCH_MOVE:
            x, y = self._abs_xy(nx, ny)
            self._rd.NotifyPointerMotionAbsolute(session, empty, node, dbus.Double(x), dbus.Double(y))
        elif ev_type == TOUCH_UP:
            self._rd.NotifyPointerButton(session, empty, dbus.Int32(BTN_LEFT_CODE), dbus.UInt32(0))
        elif ev_type == TOUCH_RIGHT_CLICK:
            x, y = self._abs_xy(nx, ny)
            self._rd.NotifyPointerMotionAbsolute(session, empty, node, dbus.Double(x), dbus.Double(y))
            self._rd.NotifyPointerButton(session, empty, dbus.Int32(BTN_RIGHT_CODE), dbus.UInt32(1))
            self._rd.NotifyPointerButton(session, empty, dbus.Int32(BTN_RIGHT_CODE), dbus.UInt32(0))
        elif ev_type == TOUCH_SCROLL:
            # nx, ny carry scroll delta steps when ev_type == TOUCH_SCROLL
            self._rd.NotifyPointerAxis(session, empty, dbus.Double(nx), dbus.Double(ny))

    def _handle_as_touch(
        self, ev_type: int, nx: float, ny: float, session: str, node: Any, empty: Any
    ) -> None:
        import dbus

        slot = dbus.UInt32(0)
        if ev_type == TOUCH_DOWN:
            x, y = self._abs_xy(nx, ny)
            self._rd.NotifyTouchDown(session, empty, node, slot, dbus.Double(x), dbus.Double(y))
            self._touch_active = True
        elif ev_type == TOUCH_MOVE and self._touch_active:
            x, y = self._abs_xy(nx, ny)
            self._rd.NotifyTouchMotion(session, empty, node, slot, dbus.Double(x), dbus.Double(y))
        elif ev_type == TOUCH_UP and self._touch_active:
            self._rd.NotifyTouchUp(session, empty, slot)
            self._touch_active = False

    def close(self) -> None:
        """Release any held button or touch slot cleanly."""
        if not self._touch_active:
            return
        try:
            import dbus

            self._rd.NotifyTouchUp(self.stream.session_handle, dbus.Dictionary(signature="sv"), dbus.UInt32(0))
        except Exception:
            pass
        self._touch_active = False


def create_input_controller(
    stream: PortalStreamInfo, virtual_output: str | None = None
) -> PortalRemoteInput | UinputTouchDevice:
    """Prefer scoped KDE Portal RemoteDesktop input when granted; otherwise /dev/uinput mapped to virtual_output."""
    if stream.has_remote_desktop and stream.rd_iface is not None:
        return PortalRemoteInput(stream)
    return UinputTouchDevice(stream.logical_width, stream.logical_height, virtual_output=virtual_output)
