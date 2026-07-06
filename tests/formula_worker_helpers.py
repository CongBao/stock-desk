from __future__ import annotations

from multiprocessing.connection import Connection
import os
import struct
import time


def posix_partial_frame_worker(
    sent: object, connection: Connection, _request: bytes
) -> None:
    if os.name == "nt":
        raise RuntimeError("Windows message-mode pipes have no stream frame semantics")
    connection._send(struct.pack("!i", 32) + b"\x00partial")
    sent.set()  # type: ignore[attr-defined]
    time.sleep(10.0)


def echo_formula_worker(connection: Connection, request: bytes) -> None:
    connection.send_bytes(b"\x00" + request)
    connection.close()
