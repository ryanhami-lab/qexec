"""R21 reproduction/proof: the test suite blocks outbound network connections.

The external review found that nothing prevented a test (or imported code) from reaching the
network. ``tests/conftest.py`` installs a session-wide guard; this test proves it raises on a
real outbound ``connect`` while leaving local connections usable.
"""

from __future__ import annotations

import socket

import pytest


def test_outbound_connect_is_blocked() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="network access is forbidden") as excinfo:
            s.connect(("93.184.216.34", 80))  # example.com; must never be dialled
        assert type(excinfo.value).__name__ == "OfflineConnectionError"
    finally:
        s.close()


def test_outbound_connect_ex_is_blocked() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="network access is forbidden") as excinfo:
            s.connect_ex(("8.8.8.8", 53))
        assert type(excinfo.value).__name__ == "OfflineConnectionError"
    finally:
        s.close()


def test_local_connection_is_permitted() -> None:
    # A loopback connect to a closed port is permitted by the guard (it fails with the OS error,
    # not the offline guard) -- proving the guard targets internet access, not all sockets.
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect(("127.0.0.1", port))  # must NOT raise OfflineConnectionError
    finally:
        client.close()
        server.close()
