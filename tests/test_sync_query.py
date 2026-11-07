"""Sync into the index and query it, end to end over a fake IMAP server."""

import imaplib
import os
from email.message import EmailMessage
from pathlib import Path

import pytest
from fakeimap import FakeIMAPServer

from readonly_safe_mail_mcp import index, query
from readonly_safe_mail_mcp.config import Account, Config, Folders, load_default_rules, merge_rules
from readonly_safe_mail_mcp.imap import MailSource, _ReadOnlyGuard
from readonly_safe_mail_mcp.sync import SyncBusy, sync_all, sync_lock


class GuardedIMAP4(_ReadOnlyGuard, imaplib.IMAP4):
    pass


def msg(
    subject,
    body,
    *,
    sender="Dana Reyes <dana@example.org>",
    day=1,
    msgid=None,
    reply_to=None,
    attach=False,
) -> bytes:
    m = EmailMessage()
    m["From"] = sender
    m["To"] = "me@example.com"
    m["Subject"] = subject
    m["Date"] = f"Mon, {day:02d} Sep 2026 10:00:00 -0700"
    if msgid:
        m["Message-ID"] = msgid
    if reply_to:
        m["In-Reply-To"] = reply_to
        m["References"] = reply_to
    m.set_content(body)
    if attach:
        m.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="budget.pdf")
    return m.as_bytes()


def make_cfg(tmp: Path, keywords=()) -> Config:
    rules = merge_rules(
        load_default_rules(), [("t", {"sensitive_keywords": list(keywords)})] if keywords else []
    )
    acct = Account("icloud", "127.0.0.1", 0, "me@example.com", Folders(), rules)
    return Config({"icloud": acct}, 300, "ask", (), tmp / "cfg", tmp / "data")


def run_sync(cfg, server, **kw):
    def open_source(acct, pw):
        src = MailSource(
            GuardedIMAP4("127.0.0.1", server.port, timeout=5),
            account=acct.name,
            folders=acct.folders,
            max_part_bytes=acct.rules.max_message_bytes,
        )
        src.login(acct.username, pw)
        return src

    return sync_all(cfg, open_source=open_source, password_fn=lambda _: "pw", **kw)


def db_for(cfg):
    return index.connect(index.index_path(cfg), create=False)


DRAFT = msg(
    "Donation ask letter draft",
    "Here is the draft of the donation ask letter.\n\nPrinting will cost about $450 for 500 copies.",
    msgid="<a1@x>",
    attach=True,
)


@pytest.fixture
def mailbox():
    return {
        "INBOX": {
            1: DRAFT,
            2: msg(
                "Your verification code",
                "Your code is 482913",
                sender="Apple <no_reply@appleid.apple.com>",
                day=2,
            ),
            3: msg(
                "Lunch",
                "See https://evil.example/reset?t=abc123 for the menu",
                day=3,
                sender="Bob <bob@example.com>",
            ),
            4: msg(
                "Re: Donation ask letter draft",
                "Postage adds $210. Total cost is under budget.",
                day=4,
                msgid="<a2@x>",
                reply_to="<a1@x>",
            ),
        },
        "Archive": {7: DRAFT},  # same message filed twice
        "Trash": {1: msg("donation trash", "should not be indexed")},
    }


def test_sync_then_search(tmp_path, mailbox):
    cfg = make_cfg(tmp_path)
    with FakeIMAPServer(mailbox) as server:
        [rep] = run_sync(cfg, server)
    assert rep.error is None and rep.added == 5
    assert set(rep.folders) == {"INBOX", "Archive"}  # Trash denied by default
    db = db_for(cfg)
    hits = query.search(db, query="donation letter")
    assert len(hits) == 2  # Archive copy de-duplicated by Message-ID
    assert all("trash" not in h.lower() for h in hits)
    assert "📎" in next(h for h in hits if "Re:" not in h)
    assert query.search(db, query="donation", from_="dana", since="2026-09-03")[0].count("Re:") == 1


def test_security_mail_withheld_and_not_searchable(tmp_path, mailbox):
    cfg = make_cfg(tmp_path)
    with FakeIMAPServer(mailbox) as server:
        run_sync(cfg, server)
    db = db_for(cfg)
    assert query.search(db, query="482913") == []
    [hit] = query.search(db, query="verification")  # subject still searchable
    assert query.WITHHELD_SNIPPET in hit
    raw = db.execute("SELECT group_concat(body, ' ') FROM messages").fetchone()[0]
    assert "482913" not in raw and "evil.example" not in raw and "abc123" not in raw


def test_passages_read_thread(tmp_path, mailbox):
    cfg = make_cfg(tmp_path)
    with FakeIMAPServer(mailbox) as server:
        run_sync(cfg, server)
    db = db_for(cfg)
    ids = [int(h.split(" ", 1)[0][1:]) for h in query.search(db, query="donation")]
    passages = query.find_passages(db, ids, "cost")
    assert any("$450" in p for p in passages) and any("$210" in p for p in passages)
    assert not any("Here is the draft" in p for p in passages)  # only matching paragraphs
    first = min(ids)
    text = query.read_message(db, first)
    assert "budget.pdf" in text and "Subject: Donation ask letter draft" in text
    thread = query.get_thread(db, first)
    assert len(thread) == 2 and "Postage" in thread[1]
    assert "<a1@x>" not in "".join(thread)  # message ids never shown


def test_incremental_deletion_and_uidvalidity(tmp_path, mailbox):
    cfg = make_cfg(tmp_path)
    with FakeIMAPServer(mailbox) as server:
        run_sync(cfg, server)
    mailbox["INBOX"][9] = msg("New board meeting", "agenda attached", day=9)
    del mailbox["INBOX"][3]
    with FakeIMAPServer(mailbox) as server:
        [rep] = run_sync(cfg, server)
        part_cmds = [c for c in server.commands if "BODY.PEEK[1" in c]
    assert rep.added == 1 and rep.removed == 1
    assert all("9" == c.split()[2] for c in part_cmds)  # only the new message was fetched
    db = db_for(cfg)
    assert query.search(db, query="lunch") == [] and len(query.search(db, query="board")) == 1
    with FakeIMAPServer(mailbox, uidvalidity={"INBOX": 99}) as server:
        [rep] = run_sync(cfg, server)
    assert rep.added == 4  # INBOX re-indexed from scratch


def test_rule_change_selects_new_generation(tmp_path, mailbox):
    cfg = make_cfg(tmp_path)
    with FakeIMAPServer(mailbox) as server:
        run_sync(cfg, server)
    old = index.index_path(cfg)
    stricter = make_cfg(tmp_path, keywords=["postage"])
    assert index.index_path(stricter) != old
    assert db_for(stricter) is None  # nothing served until resynced under the new rules
    with FakeIMAPServer(mailbox) as server:
        run_sync(stricter, server)
    assert not old.exists()  # old generation deleted
    db = db_for(stricter)
    assert (
        query.search(db, query="postage") == []
        or query.WITHHELD_SNIPPET in query.search(db, query="postage")[0]
    )
    assert "$210" not in db.execute("SELECT group_concat(body) FROM messages").fetchone()[0]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_files_are_owner_only(tmp_path, mailbox):
    cfg = make_cfg(tmp_path)
    with FakeIMAPServer(mailbox) as server:
        run_sync(cfg, server)
    assert os.stat(cfg.data_dir).st_mode & 0o777 == 0o700
    for p in cfg.data_dir.glob("index-*"):
        assert os.stat(p).st_mode & 0o077 == 0, p.name


def test_one_sync_at_a_time(tmp_path):
    cfg = make_cfg(tmp_path)
    with sync_lock(cfg), pytest.raises(SyncBusy):
        sync_all(cfg)


def test_status_and_warnings(tmp_path, mailbox):
    cfg = make_cfg(tmp_path)
    assert "not synced yet" in query.status_lines(index.account_status(None, ["icloud"]), 300)[0]
    with FakeIMAPServer(mailbox) as server:
        run_sync(cfg, server)
    db = db_for(cfg)
    [line] = query.status_lines(index.account_status(db, ["icloud"]), 300)
    assert "5 messages indexed, fully synced" in line
    assert query.warnings(index.account_status(db, ["icloud"]), 300) == []


def test_bad_fts_syntax_falls_back(tmp_path, mailbox):
    cfg = make_cfg(tmp_path)
    with FakeIMAPServer(mailbox) as server:
        run_sync(cfg, server)
    assert len(query.search(db_for(cfg), query='donation "ask (letter')) >= 1


def test_parallel_sync_interrupt_and_resume(tmp_path):
    import threading

    box = {"INBOX": {u: msg(f"note {u}", f"body {u}", day=1 + u % 28) for u in range(1, 451)}}
    cfg = make_cfg(tmp_path)
    stop = threading.Event()

    def progress(account, folder, done, total):
        if done >= 200:
            stop.set()

    with FakeIMAPServer(box) as server:
        [rep] = run_sync(cfg, server, progress=progress, stop=stop, connections=3)
    db = db_for(cfg)
    st = index.folder_state(db, "icloud", "INBOX")
    indexed = {r[0] for r in db.execute("SELECT uid FROM messages")}
    assert not st.complete and st.high_uid == 450
    assert indexed == set(range(st.low_uid, 451))  # contiguous newest-first range, no gaps
    with FakeIMAPServer(box) as server:
        [rep2] = run_sync(cfg, server, connections=3)
    assert rep2.added == 450 - len(indexed)
    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 450
    assert index.folder_state(db, "icloud", "INBOX").complete


def test_status_not_complete_until_every_folder_done(tmp_path, mailbox):
    import threading

    cfg = make_cfg(tmp_path)
    stop = threading.Event()

    def progress(account, folder, done, total):
        if folder == "INBOX" and done == total:
            stop.set()

    with FakeIMAPServer(mailbox) as server:
        run_sync(cfg, server, progress=progress, stop=stop)
    db = db_for(cfg)
    [st] = index.account_status(db, ["icloud"])
    assert st.folders == 2 and not st.complete  # Archive is pending, not "fully synced"
