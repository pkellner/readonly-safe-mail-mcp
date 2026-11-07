"""The read-only IMAP layer, tested against commands actually sent over a socket."""

import imaplib
import time
from email.message import EmailMessage

import pytest
from fakeimap import FakeIMAPServer

from readonly_safe_mail_mcp.config import Folders
from readonly_safe_mail_mcp.imap import (
    HEADER_ITEM,
    STRUCTURE_ITEM,
    Approval,
    MailSource,
    PolicyError,
    _ReadOnlyGuard,
    check_command,
    quote_mailbox,
)


class GuardedIMAP4(_ReadOnlyGuard, imaplib.IMAP4):
    """Plain-socket variant for tests only. Production uses GuardedIMAP4_SSL."""


def msg(subject="Hello", body="Hi there", attach=False) -> bytes:
    m = EmailMessage()
    m["From"] = "Friend <friend@example.com>"
    m["To"] = "me@example.com"
    m["Subject"] = subject
    m["Date"] = "Fri, 06 Nov 2026 10:00:00 -0800"
    m.set_content(body)
    if attach:
        m.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="invoice.pdf")
    return m.as_bytes()


def connect(server, *, folders=Folders(), max_part_bytes=5_000_000) -> MailSource:
    conn = GuardedIMAP4("127.0.0.1", server.port, timeout=5)
    src = MailSource(conn, account="test", folders=folders, max_part_bytes=max_part_bytes)
    src.login("me@example.com", "secret-pw")
    return src


def part_fetches(server) -> list[str]:
    return [c for c in server.commands if "BODY.PEEK[" in c and "HEADER" not in c]


# --- guard (pure) -----------------------------------------------------------


@pytest.mark.parametrize(
    "name,args",
    [
        ("CAPABILITY", ()),
        ("LOGOUT", ()),
        ("LOGIN", ("me@example.com", '"pw"')),
        ("LIST", ('""', "*")),
        ("EXAMINE", ('"INBOX"',)),
        ("UID", ("SEARCH", "ALL")),
        ("UID", ("SEARCH", "UID", "100:*", "SINCE", "6-Nov-2026")),
        ("UID", ("FETCH", "1,2,3", HEADER_ITEM)),
        ("UID", ("FETCH", "5", STRUCTURE_ITEM)),
        ("UID", ("FETCH", "5", "(UID BODY.PEEK[1.2]<0.5000001>)")),
    ],
)
def test_guard_allows_exact_production_shapes(name, args):
    check_command(name, args)


@pytest.mark.parametrize(
    "name,args",
    [
        ("SELECT", ('"INBOX"',)),
        ("STORE", ("1", "+FLAGS", "(\\Deleted)")),
        ("COPY", ("1", '"Trash"')),
        ("MOVE", ("1", '"Trash"')),
        ("EXPUNGE", ()),
        ("APPEND", ('"INBOX"', "{10}")),
        ("DELETE", ('"INBOX"',)),
        ("CREATE", ('"x"',)),
        ("RENAME", ('"a"', '"b"')),
        ("STATUS", ('"INBOX"', "(MESSAGES)")),
        ("AUTHENTICATE", ("PLAIN",)),
        ("STARTTLS", ()),
        ("ENABLE", ("UTF8=ACCEPT",)),
        ("FETCH", ("1", STRUCTURE_ITEM)),  # non-UID fetch
        ("UID", ("STORE", "1", "+FLAGS", "(\\Seen)")),
        ("UID", ("COPY", "1", '"x"')),
        ("UID", ("MOVE", "1", '"x"')),
        ("UID", ("EXPUNGE", "1")),
        ("UID", ("FETCH", "1", "(UID BODY.PEEK[])")),  # whole message
        ("UID", ("FETCH", "1", "(UID RFC822)")),
        ("UID", ("FETCH", "1", "(UID BODY[1]<0.10>)")),  # non-PEEK would set \Seen
        ("UID", ("FETCH", "1", "(UID BODY.PEEK[1])")),  # unbounded part fetch
        ("UID", ("FETCH", "1:*", STRUCTURE_ITEM)),
        ("UID", ("FETCH", "1", "(UID BODY.PEEK[1;STORE]<0.10>)")),
        ("UID", ("SEARCH", "TEXT", '"482913"')),  # server-side body search is not a capability
        ("EXAMINE", ('"INBOX" DELETE "x"',)),
        ("LIST", ('""', "%")),
    ],
)
def test_guard_refuses_everything_else(name, args):
    with pytest.raises(PolicyError):
        check_command(name, args)


def test_guard_error_never_contains_arguments():
    with pytest.raises(PolicyError) as e:
        check_command("LOGIN", ("user", '"hunter2"', "extra"))
    assert "hunter2" not in str(e.value)


@pytest.mark.parametrize("bad", ["", "IN\r\nBOX", "a\x00", "Café", "x" * 256])
def test_mailbox_names_rejected(bad):
    with pytest.raises(PolicyError):
        quote_mailbox(bad)


def test_mailbox_quoting_escapes_injection():
    q = quote_mailbox('INBOX" DELETE "x')
    check_command("EXAMINE", (q,))  # stays a single quoted argument
    assert q == '"INBOX\\" DELETE \\"x"'


# --- guard (on a live connection) -------------------------------------------


def test_imaplib_write_methods_blocked_before_sending():
    with FakeIMAPServer({"INBOX": {1: msg()}}) as server:
        src = connect(server)
        src.examine("INBOX")
        conn = src._conn
        for call in (
            lambda: conn.store("1", "+FLAGS", "(\\Deleted)"),
            lambda: conn.uid("STORE", "1", "+FLAGS", "(\\Seen)"),
            lambda: conn.copy("1", "Trash"),
            lambda: conn.expunge(),
            lambda: conn.select("INBOX"),
            lambda: conn.uid("FETCH", "1", "(BODY.PEEK[])"),
            lambda: conn.append("INBOX", None, None, b"x"),
        ):
            with pytest.raises(PolicyError):
                call()
        src.close()
    sent = " ".join(server.commands)
    for verb in ("STORE", "COPY", "EXPUNGE", "SELECT", "APPEND", "BODY.PEEK[]"):
        assert verb not in sent


def test_mailsource_requires_guarded_connection():
    with pytest.raises(TypeError):
        MailSource(object(), account="x", folders=Folders(), max_part_bytes=10)


# --- narrow methods: exact wire traffic -------------------------------------


def test_fetch_message_wire_sequence_never_touches_attachments():
    with FakeIMAPServer({"INBOX": {1: msg(attach=True)}}) as server:
        with connect(server) as src:
            info = src.examine("INBOX")
            assert info.uidvalidity == 42
            assert src.search_uids() == [1]
            fm = src.fetch_message(1)
    # Newer imaplib releases reuse the greeting's capabilities and skip CAPABILITY.
    assert [c for c in server.commands if c != "CAPABILITY"] == [
        "LOGIN me@example.com ***",
        'LIST "" *',
        'EXAMINE "INBOX"',
        "UID SEARCH ALL",
        f"UID FETCH 1 {HEADER_ITEM}",
        f"UID FETCH 1 {STRUCTURE_ITEM}",
        "UID FETCH 1 (UID BODY.PEEK[1]<0.5000001>)",
        "LOGOUT",
    ]
    assert list(fm.text_parts) == ["1"]
    assert [a.filename for a in fm.structure.attachments()] == ["invoice.pdf"]


def test_text_part_method_refuses_attachment_part():
    with FakeIMAPServer({"INBOX": {1: msg(attach=True)}}) as server:
        with connect(server) as src:
            src.examine("INBOX")
            with pytest.raises(PolicyError, match="approval"):
                src.fetch_text_part(1, "2")
            for bad in ("0", "1;STORE", "1..2", "", "BODY"):
                with pytest.raises(PolicyError):
                    src.fetch_text_part(1, bad)
    assert part_fetches(server) == []


def test_oversized_text_part_returns_none_without_reading_it_all():
    big = "word " * 2000
    with FakeIMAPServer({"INBOX": {1: msg(body=big)}}) as server:
        with connect(server, max_part_bytes=1000) as src:
            src.examine("INBOX")
            fm = src.fetch_message(1)
    assert fm.text_parts["1"] is None
    assert part_fetches(server) == ["UID FETCH 1 (UID BODY.PEEK[1]<0.1001>)"]


def test_folder_policy():
    flags = {"Junk": ("\\Junk",), "Sent": ("\\Sent",), "Private": ("\\HasNoChildren",)}
    folders = {"INBOX": {}, "Junk": {}, "Sent": {}, "Private": {}}
    with FakeIMAPServer(folders, flags=flags) as server:
        with connect(server, folders=Folders(allow=("*",), deny=("Private",))) as src:
            listed = {name: ok for name, _, ok in src.list_folders()}
            assert listed == {"INBOX": True, "Junk": False, "Sent": True, "Private": False}
            for name in ("Junk", "Private", "DoesNotExist"):
                with pytest.raises(PolicyError):
                    src.examine(name)
    assert [c for c in server.commands if c.startswith("EXAMINE")] == []


def test_search_min_uid_filters_server_quirk():
    with FakeIMAPServer({"INBOX": {1: msg(), 2: msg()}}) as server:
        with connect(server) as src:
            src.examine("INBOX")
            assert src.search_uids(min_uid=50) == []  # server returns max UID for "50:*"


def test_unparseable_structure_is_isolated_per_message():
    override = {("INBOX", 2): b'("text" garbage'}
    with FakeIMAPServer({"INBOX": {1: msg(), 2: msg(), 3: msg()}}, structure_override=override) as server:
        with connect(server) as src:
            src.examine("INBOX")
            out = src.fetch_structures([1, 2, 3])
    assert not isinstance(out[2], type(out[1]))
    assert type(out[1]).__name__ == "Structure" and type(out[3]).__name__ == "Structure"


# --- approved attachments ---------------------------------------------------


def approval(**kw) -> Approval:
    base = dict(
        account="test",
        folder="INBOX",
        uidvalidity=42,
        uid=1,
        part_id="2",
        request_id="r1",
        expires_at=time.time() + 60,
    )
    base.update(kw)
    return Approval(**base)


def test_approved_attachment_fetched():
    with FakeIMAPServer({"INBOX": {1: msg(attach=True)}}) as server:
        with connect(server) as src:
            data = src.fetch_approved_attachment(approval())
    assert data.startswith(b"JVBERi")  # base64 of %PDF (transfer-encoded; decoding happens in the worker)
    assert part_fetches(server) == ["UID FETCH 1 (UID BODY.PEEK[2]<0.20000001>)"]


@pytest.mark.parametrize(
    "kw,reason",
    [
        ({"account": "other"}, "different account"),
        ({"expires_at": 0}, "expired"),
        ({"uidvalidity": 41}, "mailbox changed"),
        ({"part_id": "1"}, "not an attachment"),
        ({"part_id": "9"}, "not an attachment"),
        ({"part_id": "2;STORE"}, "invalid part id"),
    ],
)
def test_bad_approvals_refused_and_nothing_fetched(kw, reason):
    with FakeIMAPServer({"INBOX": {1: msg(attach=True)}}) as server:
        with connect(server) as src:
            with pytest.raises(PolicyError, match=reason):
                src.fetch_approved_attachment(approval(**kw))
    assert part_fetches(server) == []
