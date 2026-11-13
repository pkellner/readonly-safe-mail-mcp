"""With contacts and calendars off, the server and CLI behave exactly as before issue #1
(Acceptance, "Mail untouched"). The expected values were taken from the code before contacts existed."""

import imaplib
import sys
from email.message import EmailMessage

import anyio
import pytest
from fakeimap import FakeIMAPServer

from readonly_safe_mail_mcp import cli, index, server
from readonly_safe_mail_mcp.config import load_config
from readonly_safe_mail_mcp.imap import MailSource, _ReadOnlyGuard
from readonly_safe_mail_mcp.sync import sync_all

INSTRUCTIONS = (
    "Read-only search over the user's email (local sanitized index). Links and codes are removed and "
    "account-security emails are withheld by design; do not try to recover them. Workflow: search -> "
    "find_passages or read_message on the ids you need. Attachments: request_attachments with the id "
    "and part shown by read_message; the user approves each."
)


def doc(text: str) -> str:
    """A description as the client sees it: from Python 3.13 the compiler strips the common
    leading whitespace from a docstring's continuation lines (the 8 spaces of server.py)."""
    return text.replace("\n        ", "\n") if sys.version_info >= (3, 13) else text


# (name, description, {argument: type}, required, read_only_hint, idempotent_hint)
TOOLS = [
    ("list_accounts", "Accounts, sync status, and indexed message counts.", {}, [], True, True),
    (
        "search",
        doc(
            'Search email. query: words (all must match), "exact phrase", OR, NOT, prefix*.\n'
            "        from_/to: substring of name or address. since/until: YYYY-MM-DD. sort: relevance|date.\n"
            "        One line per hit: #id | account | date | from | subject | snippet."
        ),
        {
            "query": "string",
            "from_": "string",
            "to": "string",
            "since": "string",
            "until": "string",
            "account": "string",
            "folder": "string",
            "sort": "string",
            "limit": "integer",
            "offset": "integer",
        },
        [],
        True,
        True,
    ),
    (
        "find_passages",
        "Only the paragraphs of the given messages that mention query's words.",
        {"ids": "array", "query": "string"},
        ["ids", "query"],
        True,
        True,
    ),
    (
        "read_message",
        "Full sanitized text of one message (quoted replies removed).",
        {"id": "integer"},
        ["id"],
        True,
        True,
    ),
    (
        "get_thread",
        "The conversation containing a message, oldest first, de-duplicated.",
        {"id": "integer"},
        ["id"],
        True,
        True,
    ),
    (
        "request_attachments",
        doc(
            "Open attachments (id + part from read_message). The user approves each in a dialog;\n"
            "        approved ones are read in a sandbox and returned as sanitized text or images."
        ),
        {"items": "array"},
        ["items"],
        True,
        False,
    ),
]

ACCOUNTS = "accounts:\n  icloud: {preset: icloud, username: me@example.com}\n"
CONTACTS_OFF = "accounts:\n  icloud: {preset: icloud, username: me@example.com, contacts: false}\n"
BOTH_OFF = (
    "calendar_years: 30\n"
    "accounts:\n  icloud: {preset: icloud, username: me@example.com, contacts: false, calendars: false}\n"
)


class GuardedIMAP4(_ReadOnlyGuard, imaplib.IMAP4):
    pass


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    (tmp_path / "cfg").mkdir()
    monkeypatch.setenv("RSMM_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("RSMM_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def write_accounts(dirs, text):
    (dirs / "cfg" / "accounts.yaml").write_text(text)


def tool_table(mcp):
    out = []
    for t in anyio.run(mcp.list_tools):
        s = t.input_schema
        props = {k: v.get("type") for k, v in s.get("properties", {}).items()}
        a = t.annotations
        out.append((t.name, t.description, props, s.get("required", []), a.read_only_hint, a.idempotent_hint))
    return out


def call(mcp, name, args=None) -> str:
    result = anyio.run(mcp.call_tool, name, args or {})
    return "\n".join(c.text for c in result.content)


@pytest.mark.parametrize("accounts", [ACCOUNTS, CONTACTS_OFF, BOTH_OFF])
def test_tool_list_and_instructions_unchanged(dirs, accounts):
    write_accounts(dirs, accounts)
    mcp = server.build_server()
    assert tool_table(mcp) == TOOLS
    assert mcp.instructions == INSTRUCTIONS


CONTACTS_ON = "accounts:\n  icloud: {preset: icloud, username: me@example.com, contacts: true}\n"
CALENDARS_ON = (
    "calendar_years: 50\naccounts:\n  icloud: {preset: icloud, username: me@example.com, calendars: true}\n"
)
CALENDARS_SOME = (
    "accounts:\n  icloud: {preset: icloud, username: me@example.com, calendars: {allow: [Work]}}\n"
)


@pytest.mark.parametrize(
    "accounts", [ACCOUNTS, CONTACTS_OFF, BOTH_OFF, CONTACTS_ON, CALENDARS_ON, CALENDARS_SOME]
)
def test_mail_index_keeps_its_name(dirs, accounts):
    write_accounts(dirs, accounts)
    assert index.index_path(load_config()).name == "index-2e537c89b600cdce.sqlite"


@pytest.mark.parametrize("contacts", ["", ", contacts: false", ", contacts: false, calendars: false"])
def test_list_accounts_and_status_unchanged(dirs, capsys, contacts):
    m = EmailMessage()
    m["From"] = "Dana Reyes <dana@example.org>"
    m["To"] = "me@example.com"
    m["Subject"] = "Garden plans"
    m["Date"] = "Mon, 01 Sep 2026 10:00:00 -0700"
    m.set_content("The tomatoes are in.")
    with FakeIMAPServer({"INBOX": {1: m.as_bytes()}}) as imap:
        write_accounts(
            dirs,
            f"accounts:\n  icloud: {{host: 127.0.0.1, port: {imap.port}, username: me@example.com{contacts}}}\n",
        )
        mcp = server.build_server()
        assert call(mcp, "list_accounts") == "icloud: 0 messages indexed, not synced yet; last sync never"

        def open_source(acct, pw):
            src = MailSource(
                GuardedIMAP4("127.0.0.1", imap.port, timeout=5),
                account=acct.name,
                folders=acct.folders,
                max_part_bytes=acct.rules.max_message_bytes,
            )
            src.login(acct.username, pw)
            return src

        cfg = load_config()
        sync_all(cfg, open_source=open_source, password_fn=lambda _: "pw")
    assert call(mcp, "list_accounts") == "icloud: 1 messages indexed, fully synced; last sync just now"
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    assert capsys.readouterr().out == (
        f"icloud: 1 messages indexed, fully synced; last sync just now\nindex: {index.index_path(cfg)}\n"
    )
    assert sorted(p.name for p in cfg.data_dir.iterdir() if p.suffix == ".sqlite") == [
        index.index_path(cfg).name
    ]
