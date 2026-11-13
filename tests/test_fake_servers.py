"""The fake servers stop at once. On Linux, closing a listening socket does not wake a
thread blocked in accept(), so a fake that relied on close() waited out its join timeout
after every test (CI, ubuntu: 5 s per test)."""

import socket
import time

import fakedav
import pytest
from fakeimap import FakeIMAPServer

FAKES = {
    "imap": lambda: FakeIMAPServer({"INBOX": {}}),
    "dav": lambda: fakedav.FakeDAVServer(fakedav.tls()[0]),
}


@pytest.mark.parametrize("make", FAKES.values(), ids=FAKES.keys())
def test_fake_server_stops_when_close_does_not_wake_accept(monkeypatch, make):
    server = make().__enter__()
    real_close = socket.socket.close

    def close(sock):
        if sock is not server._sock:  # the listening socket behaves as on Linux
            real_close(sock)

    monkeypatch.setattr(socket.socket, "close", close)
    start = time.monotonic()
    try:
        server.__exit__(None, None, None)
    finally:
        monkeypatch.undo()
        server._sock.close()
    assert not server._thread.is_alive()
    assert time.monotonic() - start < 2
