"""MCP server: search-triggered sync must not make every search wait."""

import threading
import time

import anyio

from readonly_safe_mail_mcp import server


def test_only_the_search_that_starts_a_sync_waits(tmp_path, monkeypatch):
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "accounts.yaml").write_text(
        "accounts:\n  icloud: {preset: icloud, username: me@example.com}\n"
    )
    monkeypatch.setenv("RSMM_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("RSMM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(server, "FIRST_WAIT", 0.1)
    monkeypatch.setattr(server, "MAX_WAIT", 1.0)
    release = threading.Event()
    monkeypatch.setattr(server, "sync_all", lambda cfg: release.wait(10))  # a long-running sync

    mcp = server.build_server()

    async def timed_search() -> float:
        t = time.monotonic()
        await mcp.call_tool("search", {"query": "anything"})
        return time.monotonic() - t

    try:
        first = anyio.run(timed_search)
        second = anyio.run(timed_search)
    finally:
        release.set()
    assert first >= 0.9  # started the sync, waited up to MAX_WAIT
    assert second < 0.5  # sync already running: answered immediately
