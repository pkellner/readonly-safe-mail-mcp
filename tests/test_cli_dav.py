"""The command line for contacts and calendars (issue #1, item G): `check` through the
guarded DAV source, `sync` and `status` wording per source, and the written config line."""

import fakedav
import pytest
from fakedav import (
    FOREIGN,
    FakeDAVServer,
    Reply,
    addressbook_home,
    calendar_home,
    components,
    displayname,
    entry,
    multistatus,
    principal,
    resourcetype,
)

from readonly_safe_mail_mcp import cli, credentials, davindex, davsync, sync
from readonly_safe_mail_mcp.config import load_config
from readonly_safe_mail_mcp.sync import SyncBusy

PRINCIPAL = "/123/principal/"
BOOKS = "/123/carddavhome/"
CALS = "/123/calendars/"


def accounts_yaml(contacts: bool, calendars: bool, extra: str = "") -> str:
    flags = ("contacts: true, " if contacts else "") + ("calendars: true, " if calendars else "")
    return f"accounts:\n  icloud: {{ preset: icloud, {flags}username: me@example.org }}\n{extra}"


@pytest.fixture(scope="module")
def tls_pair():
    return fakedav.tls()


@pytest.fixture
def srv(tls_pair):
    with FakeDAVServer(tls_pair[0]) as s:
        yield s


@pytest.fixture
def setup(tmp_path, monkeypatch, srv, tls_pair):
    """A config with the sources a test asks for, the fake DAV server behind the DAV source,
    no mail connection and no OS keyring."""

    def configure(contacts=True, calendars=True, extra=""):
        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir(exist_ok=True)
        (cfg_dir / "accounts.yaml").write_text(accounts_yaml(contacts, calendars, extra))
        monkeypatch.setenv("RSMM_CONFIG_DIR", str(cfg_dir))
        monkeypatch.setenv("RSMM_DATA_DIR", str(tmp_path / "data"))
        return load_config()

    opened = []

    def open_source(acct, password):
        assert password == "pw"
        opened.append(acct.name)
        return srv.source(tls_pair[1])

    monkeypatch.setattr(davsync, "open_dav_source", open_source)
    monkeypatch.setattr(credentials, "get_password", lambda name: "pw")
    monkeypatch.setattr(cli, "_connection_check", lambda acct, pw: (True, ["mail ok"]))
    srv.on("PROPFIND", "/", Reply(207, multistatus(entry("/", principal(PRINCIPAL)))), host=fakedav.CONTACTS)
    srv.on("PROPFIND", "/", Reply(207, multistatus(entry("/", principal(PRINCIPAL)))), host=fakedav.CALDAV)
    srv.on(
        "PROPFIND",
        PRINCIPAL,
        Reply(207, multistatus(entry(PRINCIPAL, addressbook_home(BOOKS)))),
        host=fakedav.CONTACTS,
    )
    srv.on(
        "PROPFIND",
        PRINCIPAL,
        Reply(207, multistatus(entry(PRINCIPAL, calendar_home(CALS)))),
        host=fakedav.CALDAV,
    )
    book = entry(BOOKS + "card/", displayname("Contacts"), resourcetype("d:collection", "card:addressbook"))
    srv.on("PROPFIND", BOOKS, Reply(207, multistatus(entry(BOOKS), book)), host=fakedav.CONTACTS)
    cal = entry(
        CALS + "home/",
        displayname("Home"),
        resourcetype("d:collection", "cal:calendar"),
        components("VEVENT"),
    )
    todo = entry(
        CALS + "todo/",
        displayname("Reminders"),
        resourcetype("d:collection", "cal:calendar"),
        components("VTODO"),
    )
    srv.on(
        "PROPFIND",
        CALS,
        Reply(207, multistatus(entry(CALS), cal, cal.replace("home", "work"), todo)),
        host=fakedav.CALDAV,
    )
    configure.opened = opened
    return configure


def check(capsys) -> tuple[int, list[str]]:
    rc = cli.main(["check"])
    return rc, [ln.strip() for ln in capsys.readouterr().out.splitlines()]


# --- check ---------------------------------------------------------------------------


def test_check_logs_in_to_each_source_through_the_guard(setup, srv, capsys):
    setup()
    rc, lines = check(capsys)
    assert rc == 0
    assert "contacts: logged in over CardDAV; 1 address book(s); only read requests sent" in lines
    assert "calendars: logged in over CalDAV; 2 calendar(s); only read requests sent" in lines
    assert lines.index("mail ok") < lines.index(
        "contacts: logged in over CardDAV; 1 address book(s); only read requests sent"
    )
    assert srv.requests and {r.method for r in srv.requests} == {"PROPFIND"}  # no card or event fetched
    assert all(r.header("Authorization") for r in srv.requests)


def test_check_only_the_sources_that_are_on(setup, srv, capsys):
    setup(contacts=True, calendars=False)
    rc, lines = check(capsys)
    assert rc == 0
    assert not any(ln.startswith("calendars:") for ln in lines)
    assert {r.host for r in srv.requests} == {fakedav.CONTACTS}
    srv.requests.clear()
    setup(contacts=False, calendars=True)
    rc, lines = check(capsys)
    assert not any(ln.startswith("contacts:") for ln in lines)
    assert {r.host for r in srv.requests} == {fakedav.CALDAV}


def test_check_mail_only_never_opens_a_dav_source(setup, srv, capsys):
    setup(contacts=False, calendars=False)
    rc, lines = check(capsys)
    assert rc == 0
    assert setup.opened == [] and srv.requests == [] and srv.connections == 0
    assert not any(ln.startswith(("contacts:", "calendars:")) for ln in lines)


def test_check_login_refused_is_reported_per_source(setup, srv, capsys):
    setup()
    srv.on("PROPFIND", "/", Reply(401), host=fakedav.CONTACTS)
    rc, lines = check(capsys)
    assert rc == 1
    assert f"contacts: FAILED: {fakedav.CONTACTS}: login failed" in lines
    assert "calendars: logged in over CalDAV; 2 calendar(s); only read requests sent" in lines


def test_check_login_refused_for_both(setup, srv, capsys):
    setup()
    for host in (fakedav.CONTACTS, fakedav.CALDAV):
        srv.on("PROPFIND", "/", Reply(401), host=host)
    rc, lines = check(capsys)
    assert rc == 1
    assert [ln for ln in lines if "FAILED" in ln] == [
        f"contacts: FAILED: {fakedav.CONTACTS}: login failed",
        f"calendars: FAILED: {fakedav.CALDAV}: login failed",
    ]


def test_check_foreign_redirect_is_refused_and_nothing_is_sent_there(setup, srv, capsys):
    setup(calendars=False)
    srv.on(
        "PROPFIND",
        "/",
        Reply(302, headers=[("Location", f"https://{FOREIGN}/steal")]),
        host=fakedav.CONTACTS,
    )
    rc, lines = check(capsys)
    assert rc == 1
    assert any(ln.startswith("contacts: FAILED:") for ln in lines)
    assert FOREIGN not in {r.host for r in srv.requests}
    assert FOREIGN.encode() not in bytes(srv.received)
    assert srv.connections == 1  # the contacts host only


def test_check_incomplete_collection_list_fails(setup, srv, capsys):
    setup(calendars=False)
    bad = entry(BOOKS + "x/", displayname("X"), resourcetype("card:addressbook"), status=500)
    srv.on("PROPFIND", BOOKS, Reply(207, multistatus(entry(BOOKS), bad)), host=fakedav.CONTACTS)
    rc, lines = check(capsys)
    assert rc == 1
    assert "contacts: FAILED: address book list incomplete (an entry without status 200)" in lines


def test_check_unreachable_server_fails_cleanly(setup, srv, capsys, monkeypatch):
    setup()
    monkeypatch.setattr(davsync, "open_dav_source", lambda acct, pw: srv.source(fakedav.tls()[1]))
    rc, lines = check(capsys)  # a certificate this machine does not trust
    assert rc == 1
    assert sum("FAILED" in ln for ln in lines) == 2
    assert not any("logged in" in ln for ln in lines)


def test_check_missing_password_skips_every_source(setup, srv, capsys, monkeypatch):
    setup()

    def missing(name):
        raise credentials.CredentialError("no password")

    monkeypatch.setattr(credentials, "get_password", missing)
    rc, lines = check(capsys)
    assert rc == 1 and srv.requests == [] and srv.connections == 0


# --- sync and status wording ------------------------------------------------------------


@pytest.mark.parametrize(
    ("contacts", "calendars", "words"),
    [(True, False, "contacts"), (False, True, "calendar"), (True, True, "contacts and calendar")],
)
def test_sync_busy_says_which_sources(setup, monkeypatch, capsys, contacts, calendars, words):
    """Intended change: the message named contacts even when only calendars were on."""
    setup(contacts, calendars)
    monkeypatch.setattr(sync, "sync_all", lambda cfg, **kw: [sync.SyncReport("icloud")])

    def busy(cfg, accounts=None):
        raise SyncBusy

    monkeypatch.setattr(davsync, "sync_all", busy)
    assert cli.main(["sync"]) == 1
    err = capsys.readouterr().err
    assert f"Another {words} sync is already running (CLI or MCP server). Try again later.\n" in err
    assert f"Syncing {words}..." in err


def test_sync_mail_only_prints_nothing_about_sources(setup, monkeypatch, capsys):
    setup(contacts=False, calendars=False)
    monkeypatch.setattr(sync, "sync_all", lambda cfg, **kw: [sync.SyncReport("icloud")])
    assert cli.main(["sync"]) == 0
    cap = capsys.readouterr()
    assert "Syncing contacts" not in cap.err and "calendar" not in cap.err + cap.out


def test_source_words(setup):
    assert davindex.source_words(setup(True, False)) == "contacts"
    assert davindex.source_words(setup(False, True)) == "calendar"
    assert davindex.source_words(setup(True, True)) == "contacts and calendar"
    assert davindex.source_words(setup(False, False)) == ""


def test_status_before_the_first_sync_names_each_source_and_the_option(setup, capsys):
    setup(True, True, extra="calendar_years: 7\n")
    assert cli.main(["status"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "icloud contacts: not synced yet; last sync never" in lines
    assert "icloud calendars: not synced yet (calendar_years: 7); last sync never" in lines
    assert lines[-1].startswith("contacts and calendar index: ")


def test_status_mail_only_has_no_source_lines(setup, capsys):
    setup(False, False)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "contacts" not in out and "calendar" not in out


# --- the line the installer writes ------------------------------------------------------


@pytest.mark.parametrize(
    ("flags", "contacts", "calendars"),
    [
        ("", False, False),
        (", contacts: true", True, False),
        (", contacts: true, calendars: true", True, True),
    ],
)
def test_installer_line_loads(tmp_path, monkeypatch, flags, contacts, calendars):
    """The exact shapes install.sh writes, including its commented calendar_years block."""
    (tmp_path / "accounts.yaml").write_text(
        f'accounts:\n  icloud: {{ preset: icloud, username: "me@example.org"{flags} }}\n'
        "\n# Years of calendar searched before and after this month (1 to 50).\n"
        "# Remove the # and change the number to search further back or ahead.\n# calendar_years: 10\n"
    )
    cfg = load_config(tmp_path, tmp_path / "data")
    assert cfg.calendar_years == 10
    acct = cfg.accounts["icloud"]
    assert acct.contacts is contacts and (acct.calendars is not None) is calendars
    (tmp_path / "accounts.yaml").write_text(
        (tmp_path / "accounts.yaml").read_text().replace("# calendar_years: 10", "calendar_years: 25")
    )
    assert load_config(tmp_path, tmp_path / "data").calendar_years == 25


def test_check_names_allow_and_deny_entries_that_match_no_calendar(setup, capsys, tmp_path):
    """Calendars are chosen by name, so a typo in deny would leave that calendar indexed."""
    setup()
    (tmp_path / "cfg" / "accounts.yaml").write_text(
        "accounts:\n  icloud: { preset: icloud, username: me@example.org, "
        "calendars: { allow: [home, Hoem], deny: [Birthdays] } }\n"
    )
    rc, lines = check(capsys)
    assert rc == 0
    assert "calendars: logged in over CalDAV; 2 calendar(s); only read requests sent" in lines
    assert "calendars: allow/deny name(s) matching no calendar: Hoem, Birthdays" in lines
