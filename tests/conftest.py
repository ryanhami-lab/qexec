"""Global test configuration: block all network access during the test suite (R21).

The QExec build is **offline and zero-cost**: no test may open a network connection. This
``conftest.py`` installs a session-wide guard that raises :class:`OfflineConnectionError` from
``socket.socket.connect`` / ``connect_ex`` for any non-local address, so an accidental network
call fails loudly instead of silently reaching the internet (or a paid data API).

Loopback / AF_UNIX connections are allowed because some local tooling (and polars/pyarrow in a
few environments) may use them; the guard targets outbound internet access specifically. The
guard proves itself in ``tests/unit/test_remediation_study_offline.py``.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}


class OfflineConnectionError(RuntimeError):
    """Raised when a test attempts a non-local network connection (the build is offline)."""


def _is_local(address: Any) -> bool:
    # AF_UNIX and other non-(host, port) addresses are treated as local (not internet access).
    if not isinstance(address, tuple) or len(address) < 1:
        return True
    host = address[0]
    return host in _LOCAL_HOSTS


def _guarded_connect(self: socket.socket, address: Any) -> Any:
    if not _is_local(address):
        raise OfflineConnectionError(
            f"network access is forbidden during tests (attempted connect to {address!r}); "
            "this build is offline and zero-cost"
        )
    return _real_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> Any:
    if not _is_local(address):
        raise OfflineConnectionError(
            f"network access is forbidden during tests (attempted connect_ex to {address!r}); "
            "this build is offline and zero-cost"
        )
    return _real_connect_ex(self, address)


@pytest.fixture(autouse=True, scope="session")
def _block_network() -> Iterator[None]:
    """Install the socket guard for the whole test session."""
    socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.socket.connect = _real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = _real_connect_ex  # type: ignore[method-assign]
