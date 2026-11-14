"""Virtual PipeWire/PulseAudio sink + low-latency PCM streamer for iPad speaker output."""

from __future__ import annotations

import subprocess
import threading
from typing import Any

from protocol import PacketWriter


SINK_NAME = "ipad_monitor_audio"
SINK_DESCRIPTION = "iPad_Second_Monitor_Audio"


def _load_virtual_sink() -> str | None:
    """Create a selectable virtual audio sink in PipeWire/PulseAudio without changing default output."""
    try:
        res = subprocess.run(
            [
                "pactl",
                "load-module",
                "module-null-sink",
                f"sink_name={SINK_NAME}",
                f"sink_properties=device.description={SINK_DESCRIPTION}",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if res.returncode == 0:
            mod_id = res.stdout.strip()
            if mod_id:
                return mod_id
    except Exception as exc:
        print(f"[Audio] No se pudo crear el sink virtual ({exc}).")
    return None


def _unload_virtual_sink(module_id: str | None) -> None:
    """Unload the virtual audio sink module cleanly."""
    if not module_id:
        return
    try:
        subprocess.run(
            ["pactl", "unload-module", module_id],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        pass


class AudioStreamer:
    """Streams 48kHz stereo S16LE PCM from the virtual sink monitor to the iPad."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._pipeline: Any = None
        self._module_id: str | None = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, writer: PacketWriter) -> bool:
        """Create the virtual audio sink and start streaming PCM chunks."""
        self.stop()
        with self._lock:
            mod_id = _load_virtual_sink()
            if mod_id is None:
                return False
            self._module_id = mod_id
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(writer,),
                name="audio-streamer",
                daemon=True,
            )
            self._thread.start()
            print(f"[Audio] 🔊 Salida virtual '{SINK_DESCRIPTION}' lista en el mezclador de KDE (Módulo {mod_id}).")
            return True

    def stop(self) -> None:
        """Stop GStreamer audio capture and remove the virtual sink."""
        with self._lock:
            self._stop_event.set()
            pipe = self._pipeline
            self._pipeline = None
            thread = self._thread
            self._thread = None
            mod_id = self._module_id
            self._module_id = None

        if pipe is not None:
            try:
                import gi

                gi.require_version("Gst", "1.0")
                from gi.repository import Gst

                pipe.set_state(Gst.State.NULL)
            except Exception:
                pass
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=2.0)
        _unload_virtual_sink(mod_id)

    def _run(self, writer: PacketWriter) -> None:
        try:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst

            Gst.init(None)
            desc = (
                f"pulsesrc device={SINK_NAME}.monitor buffer-time=20000 latency-time=10000 ! "
                "audio/x-raw,format=S16LE,channels=2,rate=48000 ! "
                "appsink name=asink emit-signals=false sync=false max-buffers=4 drop=true"
            )
            pipe = Gst.parse_launch(desc)
            sink = pipe.get_by_name("asink")
            with self._lock:
                if self._stop_event.is_set():
                    pipe.set_state(Gst.State.NULL)
                    return
                self._pipeline = pipe

            if pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                print("[Audio] ❌ No se pudo iniciar pulsesrc para el audio del iPad.")
                return

            while not self._stop_event.is_set():
                sample = sink.emit("try-pull-sample", 100_000_000)  # 100ms
                if sample is None:
                    continue
                buf = sample.get_buffer()
                ok, mapinfo = buf.map(Gst.MapFlags.READ)
                if not ok:
                    continue
                try:
                    data = bytes(mapinfo.data)
                finally:
                    buf.unmap(mapinfo)
                if data and not writer.send_audio(data):
                    break
        except Exception as exc:
            print(f"[Audio] Error en stream de audio: {exc}")
        finally:
            self._stop_event.set()
