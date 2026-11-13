"""Lazy GStreamer video capture/encoding pipeline with hardware H.264 + MJPEG fallback."""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from typing import Any, Callable

from portal_session import PortalStreamInfo
from protocol import CODEC_MJPEG, PacketWriter

_gst_ready = False
_gst_lock = threading.Lock()


def ensure_gst_initialized() -> Any:
    """Lazy-import and initialize GStreamer only when a stream starts."""
    global _gst_ready
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    with _gst_lock:
        if not _gst_ready:
            Gst.init(None)
            _gst_ready = True
    return Gst


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def parse_virtual_output_from_kscreen(
    stdout: str, target_w: int = 0, target_h: int = 0
) -> tuple[str | None, bool]:
    """Parse (output_name, target_mode_already_active) from kscreen-doctor -o output."""
    clean = _ANSI_ESCAPE_RE.sub("", stdout)
    lines = clean.splitlines()
    for idx, line in enumerate(lines):
        if "Virtual" in line or "VIRTUAL" in line or "Wayland" in line:
            match = re.search(r"Output:\s*\d+\s+([^\s]+)", line)
            if match:
                out_name = match.group(1)
                block = "\n".join(lines[idx + 1 : idx + 10])
                already_active = bool(
                    target_w > 0
                    and target_h > 0
                    and re.search(rf"{target_w}x{target_h}@[\d.]+\*", block)
                )
                return out_name, already_active
    return None, False


def apply_custom_resolution(target_w: int, target_h: int) -> str | None:
    """Detect KDE Plasma virtual output (stripping ANSI escapes) and ensure iPad aspect resolution."""
    # Prefer landscape virtual desktop (UIScreen.nativeBounds always reports portrait w < h)
    even_w = max(target_w, target_h) & ~1
    even_h = min(target_w, target_h) & ~1
    try:
        out_name: str | None = None
        already_active = False
        for attempt in range(5):
            res = subprocess.run(["kscreen-doctor", "-o"], capture_output=True, text=True, timeout=3)
            out_name, already_active = parse_virtual_output_from_kscreen(res.stdout, even_w, even_h)
            if out_name is not None:
                break
            if attempt < 4:
                time.sleep(0.2)

        if out_name is not None:
            mode_str = f"{even_w}x{even_h}@60"
            if not already_active:
                print(f"[KScreen] Salida virtual extendida: {out_name}. Modo {mode_str}...")
                subprocess.run(
                    ["kscreen-doctor", f"output.{out_name}.addCustomMode.{even_w}.{even_h}.60000.full"],
                    capture_output=True,
                    timeout=3,
                )
                subprocess.run(
                    ["kscreen-doctor", f"output.{out_name}.mode.{mode_str}"],
                    capture_output=True,
                    timeout=3,
                )
            else:
                print(f"[KScreen] Salida virtual extendida activa: {out_name} ({even_w}x{even_h}).")
            return out_name
    except Exception as exc:
        print(f"[KScreen] Aviso resolución: {exc}")
    return None


def select_encoder_pipeline(gst_mod: Any, codec_pref: int, is_wifi: bool) -> tuple[str, str]:
    """Select best available encoder chain and return (pipeline_fragment, codec_name)."""
    bitrate_kbps = 10000 if is_wifi else 22000
    jpeg_quality = 65 if is_wifi else 78

    if codec_pref != CODEC_MJPEG:
        for factory_name, fmt, extra in (
            (
                "vah264lpenc",
                "NV12",
                f"vah264lpenc bitrate={bitrate_kbps} key-int-max=60 b-frames=0 target-usage=4",
            ),
            (
                "vah264enc",
                "NV12",
                f"vah264enc bitrate={bitrate_kbps} key-int-max=60 b-frames=0 target-usage=4",
            ),
            (
                "x264enc",
                "I420",
                f"x264enc tune=zerolatency speed-preset=ultrafast bitrate={bitrate_kbps} key-int-max=60 bframes=0",
            ),
        ):
            if gst_mod.ElementFactory.find(factory_name) is not None:
                chain = (
                    f"videoconvert ! video/x-raw,format={fmt} ! "
                    f"{extra} ! "
                    f"h264parse config-interval=-1 ! "
                    f"video/x-h264,profile=main,stream-format=byte-stream,alignment=au"
                )
                return chain, f"h264 ({factory_name})"

    if codec_pref != CODEC_MJPEG:
        print("[Stream] Aviso: ningún encoder H.264 disponible; usando respaldo MJPEG.")
    return f"videoconvert ! video/x-raw,format=I420 ! jpegenc quality={jpeg_quality} idct-method=ifast", "mjpeg"


def build_pipeline_string(stream: PortalStreamInfo, encoder_chain: str) -> str:
    """Assemble full GStreamer pipewiresrc -> appsink string."""
    # PipeWire 1.6+ prefers target-object serial when available; fallback to path=node_id
    source_target = (
        f'target-object="{stream.pw_serial}"' if stream.pw_serial is not None else f'path="{stream.node_id}"'
    )
    return (
        f"pipewiresrc fd={stream.pipewire_fd} {source_target} do-timestamp=true keepalive-time=1000 ! "
        f"{encoder_chain} ! "
        f"appsink name=sink emit-signals=false sync=false max-buffers=1 drop=true"
    )


class VideoStreamer:
    """Streams PipeWire screen capture over PacketWriter in a background thread."""

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._pipeline: Any = None
        self._pw_fd: int | None = None
        self._on_finished: Callable[[], None] | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(
        self,
        stream: PortalStreamInfo,
        writer: PacketWriter,
        codec_pref: int,
        is_wifi: bool,
        on_finished: Callable[[], None],
    ) -> None:
        """Start GStreamer streaming loop in a worker thread."""
        self.stop()
        self._stop_event.clear()
        self._pw_fd = stream.pipewire_fd
        self._on_finished = on_finished

        gst = ensure_gst_initialized()
        enc_chain, codec_label = select_encoder_pipeline(gst, codec_pref, is_wifi)
        pipeline_str = build_pipeline_string(stream, enc_chain)
        use_h264 = codec_label.startswith("h264")

        self._pipeline = gst.parse_launch(pipeline_str)
        sink = self._pipeline.get_by_name("sink")
        self._pipeline.set_state(gst.State.PLAYING)

        writer.send_status(f"Transmisión activa ({codec_label.upper()}, 60 FPS).")
        print(f"[Stream] ⚡ Transmitiendo vía {'Wi-Fi' if is_wifi else 'USB'} usando {codec_label}…")

        self._thread = threading.Thread(
            target=self._run_loop,
            args=(gst, sink, writer, use_h264),
            name="gst-streamer",
            daemon=True,
        )
        self._thread.start()

    def _run_loop(self, gst: Any, sink: Any, writer: PacketWriter, use_h264: bool) -> None:
        first_frame = True
        fps_count = 0
        fps_timer = time.monotonic()

        try:
            while not self._stop_event.is_set():
                # try_pull_sample with 100ms timeout avoids hang on stop()
                sample = sink.emit("try-pull-sample", 100_000_000)
                if not sample:
                    continue

                buf = sample.get_buffer()
                success, map_info = buf.map(gst.MapFlags.READ)
                if not success:
                    continue

                payload = bytes(map_info.data)
                is_keyframe = not buf.has_flags(gst.BufferFlags.DELTA_UNIT)
                buf.unmap(map_info)

                if first_frame:
                    print(f"[Stream] 🚀 Primer frame ({len(payload)} bytes, key={is_keyframe}).")
                    first_frame = False

                sent = writer.send_h264(payload, is_keyframe) if use_h264 else writer.send_mjpeg(payload)
                if not sent:
                    break

                fps_count += 1
                elapsed = time.monotonic() - fps_timer
                if elapsed >= 4.0:
                    fps = fps_count / elapsed
                    if fps < 2.0:
                        print(f"[Stream] 💤 Pantalla en reposo (sin cambios KDE): {fps:.1f} FPS")
                    else:
                        print(f"[Stream] ⚡ Transmitiendo a {fps:.1f} FPS")
                    fps_count = 0
                    fps_timer = time.monotonic()
        except Exception as exc:
            print(f"[Stream] Sesión finalizada: {exc}")
        finally:
            self._cleanup_pipeline(gst)
            if self._on_finished and not self._stop_event.is_set():
                self._on_finished()

    def _cleanup_pipeline(self, gst: Any | None = None) -> None:
        if self._pipeline is not None:
            try:
                if gst is None:
                    gst = ensure_gst_initialized()
                self._pipeline.set_state(gst.State.NULL)
            except Exception:
                pass
            self._pipeline = None
        if self._pw_fd is not None:
            try:
                os.close(self._pw_fd)
            except OSError:
                pass
            self._pw_fd = None

    def stop(self) -> None:
        """Stop video streaming loop and close PipeWire FD."""
        self._stop_event.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None
        self._cleanup_pipeline()
