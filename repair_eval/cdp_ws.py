#!/usr/bin/env python3
"""Minimal RFC 6455 WebSocket client - standard library only.

Why this exists
---------------
The managed interpreter has neither `websocket` nor `requests`, and reusing
Open-LLM-VTuber's venv would pollute a third-party dependency tree.
CDP only needs small text frames, so this is sufficient and keeps the bridge
zero-dependency (it can travel with the repo).

Scope: text frames, continuation frames, ping/pong, close. No extensions,
no permessage-deflate (CDP does not require it for our usage).
"""

from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
from urllib.parse import urlparse

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WSError(RuntimeError):
    """Raised on handshake failure or protocol violation."""


class WS:
    def __init__(self, url: str, timeout: float = 10.0):
        self.url = url
        u = urlparse(url)
        self.secure = (u.scheme or "ws") == "wss"
        self.host = u.hostname or "127.0.0.1"
        self.port = u.port or (443 if self.secure else 80)
        self.path = u.path or "/"
        if u.query:
            self.path += "?" + u.query
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self._buf = b""

    # ------------------------------------------------------------------ setup
    def connect(self) -> "WS":
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.secure:
            ctx = ssl.create_default_context()
            self.sock = ctx.wrap_socket(self.sock, server_hostname=self.host)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ) % (self.path, self.host, self.port, key)
        self.sock.sendall(req.encode("ascii"))

        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSError("connection closed during handshake")
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        self._buf = rest
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status:
            raise WSError("handshake failed: %s" % status)
        return self

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # ------------------------------------------------------------------- i/o
    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WSError("socket closed while reading")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _recv_frame(self):
        b0, b1 = self._read(2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            (length,) = struct.unpack(">H", self._read(2))
        elif length == 127:
            (length,) = struct.unpack(">Q", self._read(8))
        if masked:
            mask = self._read(4)
            payload = bytearray(self._read(length))
            for i in range(length):
                payload[i] ^= mask[i % 4]
            payload = bytes(payload)
        else:
            payload = self._read(length)
        return fin, opcode, payload

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        hdr = bytearray()
        hdr.append(0x80 | opcode)
        n = len(payload)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        body = bytearray(payload)
        for i in range(n):
            body[i] ^= mask[i % 4]
        self.sock.sendall(bytes(hdr) + bytes(body))

    def send(self, text: str) -> None:
        """Send one text message (client frames must be masked)."""
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def recv(self, timeout: float | None = None):
        """Receive exactly one complete message (continuations joined)."""
        if timeout is not None and self.sock is not None:
            self.sock.settimeout(timeout)
        while True:
            fin, opcode, payload = self._recv_frame()
            if opcode == OP_CLOSE:
                self.close()
                raise WSError("server sent close frame")
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode in (OP_TEXT, OP_BIN):
                chunks = [payload]
                while not fin:
                    fin, op2, part = self._recv_frame()
                    if op2 != OP_CONT:
                        raise WSError("bad continuation opcode %d" % op2)
                    chunks.append(part)
                data = b"".join(chunks)
                return data.decode("utf-8", "replace") if opcode == OP_TEXT else data
            raise WSError("unexpected opcode %d" % opcode)

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()
