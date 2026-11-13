"""Unit and integration tests for linux-server modules."""

from __future__ import annotations

import os
import socket
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

LINUX_SERVER_DIR = Path(__file__).resolve().parents[1]
if str(LINUX_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(LINUX_SERVER_DIR))

import config_manager
import protocol
import usbmux_client
import video_pipeline


class TestConfigManager(unittest.TestCase):
    def test_config_lifecycle_and_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
                cfg = config_manager.load_config()
                self.assertEqual(len(cfg["pin"]), 4)
                self.assertTrue(cfg["pin"].isdigit())
                self.assertGreater(len(set(cfg["pin"])), 1)
                self.assertEqual(cfg["wifi_port"], 8889)
                self.assertEqual(cfg["usb_port"], 8888)
                self.assertEqual(cfg["mode"], "auto")

                cfg_path = config_manager.get_config_path()
                mode = os.stat(cfg_path).st_mode & 0o777
                self.assertEqual(mode, 0o600)

                config_manager.set_pin("4829")
                self.assertEqual(config_manager.load_config()["pin"], "4829")
                with self.assertRaises(ValueError):
                    config_manager.set_pin("12")

                config_manager.set_restore_token("kde-tok-123")
                self.assertEqual(config_manager.get_restore_token(), "kde-tok-123")
                cfg_data = config_manager.load_config()
                self.assertEqual(cfg_data.get("restore_portal"), "screencast")

                # Legacy RemoteDesktop tokens (without restore_portal="screencast") must be ignored
                cfg_data["restore_portal"] = "remotedesktop"
                config_manager.save_config(cfg_data)
                self.assertIsNone(config_manager.get_restore_token())

                config_manager.set_restore_token(None)
                self.assertIsNone(config_manager.get_restore_token())


class TestProtocolAndFraming(unittest.TestCase):
    def test_hmac_auth(self) -> None:
        nonce = protocol.generate_nonce()
        self.assertEqual(len(nonce), 32)
        proof = protocol.compute_pin_hmac("4829", nonce)
        self.assertTrue(protocol.verify_pin_hmac("4829", nonce, proof))
        self.assertFalse(protocol.verify_pin_hmac("9999", nonce, proof))
        self.assertFalse(protocol.verify_pin_hmac("4829", nonce, b"short"))

    def test_recv_exact_fragmented_and_writer(self) -> None:
        s_a, s_b = socket.socketpair()
        try:
            writer = protocol.PacketWriter(s_a)
            self.assertTrue(writer.send_auth_challenge(b"A" * 32))
            self.assertTrue(writer.send_h264(b"\x00\x00\x00\x01\x65IDR", is_keyframe=True))
            self.assertTrue(writer.send_mjpeg(b"\xff\xd8JPEG\xff\xd9"))
            self.assertTrue(writer.send_status("Hola"))
            s_a.shutdown(socket.SHUT_WR)

            # Read OP_AUTH challenge
            head = protocol.recv_exact(s_b, 2)
            self.assertEqual(head, bytes([protocol.OP_AUTH, protocol.AUTH_REQUIRED]))
            self.assertEqual(protocol.recv_exact(s_b, 32), b"A" * 32)

            # Read OP_FRAME_H264
            op, flags, length = struct.unpack(">BBI", protocol.recv_exact(s_b, 6))
            self.assertEqual(op, protocol.OP_FRAME_H264)
            self.assertEqual(flags, protocol.H264_FLAG_KEYFRAME)
            self.assertEqual(protocol.recv_exact(s_b, length), b"\x00\x00\x00\x01\x65IDR")

            # Read OP_FRAME_MJPEG
            op, length = struct.unpack(">BI", protocol.recv_exact(s_b, 5))
            self.assertEqual(op, protocol.OP_FRAME_MJPEG)
            self.assertEqual(protocol.recv_exact(s_b, length), b"\xff\xd8JPEG\xff\xd9")

            # Read OP_STATUS
            op, length = struct.unpack(">BH", protocol.recv_exact(s_b, 3))
            self.assertEqual(op, protocol.OP_STATUS)
            self.assertEqual(protocol.recv_exact(s_b, length), b"Hola")

            with self.assertRaises(ConnectionError):
                protocol.recv_exact(s_b, 1)
        finally:
            s_a.close()
            s_b.close()


class TestUsbmuxAndInotify(unittest.TestCase):
    def test_mux_plist_roundtrip(self) -> None:
        s_a, s_b = socket.socketpair()
        try:
            payload = {"MessageType": "Connect", "DeviceID": 3, "PortNumber": socket.htons(8888)}
            s_a.sendall(usbmux_client.pack_mux_plist(payload, tag=7))
            tag, decoded = usbmux_client.read_mux_plist(s_b)
            self.assertEqual(tag, 7)
            self.assertEqual(decoded, payload)
        finally:
            s_a.close()
            s_b.close()

    def test_parse_inotify_names(self) -> None:
        name1 = b"foo\x00\x00\x00\x00\x00"
        name2 = b"usbmuxd\x00"
        ev1 = struct.pack(usbmux_client.INOTIFY_EVENT_FMT, 1, usbmux_client.IN_CREATE, 0, len(name1)) + name1
        ev2 = struct.pack(usbmux_client.INOTIFY_EVENT_FMT, 1, usbmux_client.IN_CREATE, 0, len(name2)) + name2
        self.assertEqual(usbmux_client.parse_inotify_names(ev1 + ev2), ["foo", "usbmuxd"])


class TestStandbyAndPipeline(unittest.TestCase):
    def test_lazy_imports_on_host_daemon_import(self) -> None:
        # Run in clean python interpreter to ensure Gst and dbus stay unloaded at import time
        import subprocess

        code = (
            "import sys; sys.path.insert(0, %r); "
            "import host_daemon; "
            "assert 'gi.repository.Gst' not in sys.modules, 'Gst loaded eagerly!'; "
            "assert 'dbus' not in sys.modules, 'dbus loaded eagerly!'"
            % str(LINUX_SERVER_DIR)
        )
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, msg=res.stderr)

    def test_gstreamer_try_pull_sample(self) -> None:
        gst = video_pipeline.ensure_gst_initialized()
        chain, label = video_pipeline.select_encoder_pipeline(gst, protocol.CODEC_AUTO, is_wifi=True)
        self.assertTrue(label.startswith("h264"))
        pipe = gst.parse_launch(
            f"videotestsrc num-buffers=2 ! video/x-raw,width=320,height=240,framerate=30/1 ! {chain} ! "
            f"appsink name=sink emit-signals=false sync=false max-buffers=1 drop=true"
        )
        sink = pipe.get_by_name("sink")
        pipe.set_state(gst.State.PLAYING)
        try:
            sample = sink.emit("try-pull-sample", 2_000_000_000)
            self.assertIsNotNone(sample)
            buf = sample.get_buffer()
            ok, map_info = buf.map(gst.MapFlags.READ)
            self.assertTrue(ok)
            data = bytes(map_info.data)
            buf.unmap(map_info)
            self.assertIn(b"\x00\x00\x00\x01", data)
        finally:
            pipe.set_state(gst.State.NULL)

    def test_parse_ansi_kscreen_virtual_output(self) -> None:
        sample = (
            "\x1b[01;32mOutput: \x1b[0;0m1 Virtual-virtual-xdp-kde- 7a3a163c\n"
            "\tModes:  1:1920x1080@60.00!  2:2360x1640@59.94*\n"
            "\tCustom modes:\n\t\t0: 2360x1640@59.94\n"
        )
        name, active = video_pipeline.parse_virtual_output_from_kscreen(sample, 2360, 1640)
        self.assertEqual(name, "Virtual-virtual-xdp-kde-")
        self.assertTrue(active)

        name2, active2 = video_pipeline.parse_virtual_output_from_kscreen(sample, 2560, 1600)
        self.assertEqual(name2, "Virtual-virtual-xdp-kde-")
        self.assertFalse(active2)

        sample_plain = "Output: 2 eDP-1 uuid\n\tenabled\n"
        self.assertEqual(video_pipeline.parse_virtual_output_from_kscreen(sample_plain, 2360, 1640), (None, False))


if __name__ == "__main__":
    unittest.main()
