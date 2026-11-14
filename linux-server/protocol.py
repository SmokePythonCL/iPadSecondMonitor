"""Binary framing protocol and challenge-response auth helpers."""

from __future__ import annotations

import hashlib
import hmac
import os
import socket
import struct
import threading

# Host -> Client opcodes
OP_FRAME_MJPEG = 0x01
OP_STATUS = 0x02
OP_FRAME_H264 = 0x04
OP_AUTH = 0x06
OP_AUDIO_PCM = 0x07

# Auth statuses
AUTH_REQUIRED = 0x00
AUTH_OK = 0x01
AUTH_FAILED = 0x02

# Client -> Host opcodes
OP_CMD_START = 0x01
OP_CMD_STOP = 0x02
OP_TOUCH_EVENT = 0x03
OP_CMD_ACK = 0x05
OP_AUTH_RESPONSE = 0x06
OP_CMD_RESET_PORTAL = 0x07
OP_MT_EVENT = 0x08
OP_CMD_AUDIO = 0x09

# Touch event types
TOUCH_DOWN = 0
TOUCH_MOVE = 1
TOUCH_UP = 2
TOUCH_RIGHT_CLICK = 3
TOUCH_SCROLL = 4

# Codec preferences
CODEC_AUTO = 0
CODEC_MJPEG = 1

H264_FLAG_KEYFRAME = 0x01


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes from a stream socket or raise ConnectionError on EOF."""
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("Conexión cerrada por el par remoto")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def generate_nonce() -> bytes:
    """Generate a 32-byte cryptographically random auth challenge."""
    return os.urandom(32)


def compute_pin_hmac(pin: str, nonce: bytes) -> bytes:
    """Compute HMAC-SHA256 proof of knowledge of the pairing PIN."""
    return hmac.new(pin.encode("utf-8"), nonce, hashlib.sha256).digest()


def verify_pin_hmac(pin: str, nonce: bytes, response: bytes) -> bool:
    """Verify client HMAC response in constant time."""
    if len(response) != 32:
        return False
    expected = compute_pin_hmac(pin, nonce)
    return hmac.compare_digest(expected, response)


class PacketWriter:
    """Thread-safe packet framer so concurrent threads never interleave bytes."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._lock = threading.Lock()
        self._closed = False

    def close(self) -> None:
        """Close the underlying socket safely."""
        with self._lock:
            self._closed = True
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass

    def _send_bytes(self, data: bytes) -> bool:
        with self._lock:
            if self._closed:
                return False
            try:
                self._sock.sendall(data)
                return True
            except OSError:
                self._closed = True
                return False

    def send_status(self, text: str) -> bool:
        """Send UTF-8 status banner message to the iPad."""
        payload = text.encode("utf-8")[:65535]
        header = struct.pack(">BH", OP_STATUS, len(payload))
        return self._send_bytes(header + payload)

    def send_mjpeg(self, jpeg_bytes: bytes) -> bool:
        """Send an MJPEG video frame."""
        header = struct.pack(">BI", OP_FRAME_MJPEG, len(jpeg_bytes))
        return self._send_bytes(header + jpeg_bytes)

    def send_h264(self, au_bytes: bytes, is_keyframe: bool) -> bool:
        """Send an Annex-B H.264 Access Unit with keyframe flag."""
        flags = H264_FLAG_KEYFRAME if is_keyframe else 0x00
        header = struct.pack(">BBI", OP_FRAME_H264, flags, len(au_bytes))
        return self._send_bytes(header + au_bytes)

    def send_audio(self, pcm_bytes: bytes) -> bool:
        """Send raw 48kHz S16LE stereo PCM audio chunk."""
        header = struct.pack(">BI", OP_AUDIO_PCM, len(pcm_bytes))
        return self._send_bytes(header + pcm_bytes)

    def send_auth_challenge(self, nonce: bytes) -> bool:
        """Request PIN authentication with a 32-byte nonce."""
        header = struct.pack(">BB", OP_AUTH, AUTH_REQUIRED)
        return self._send_bytes(header + nonce)

    def send_auth_result(self, ok: bool) -> bool:
        """Notify client whether authentication succeeded or failed."""
        status = AUTH_OK if ok else AUTH_FAILED
        return self._send_bytes(struct.pack(">BB", OP_AUTH, status))
