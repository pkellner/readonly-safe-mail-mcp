"""install.sh's settings handling, run with bash: which files it may rewrite, that it asks
about contacts and calendar until the answer is written down, and which closing message a
re-run shows. The installer is macOS only; these functions are plain bash and awk."""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from readonly_safe_mail_mcp.config import load_config

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="install.sh is a bash script for macOS"
)

INSTALL = (Path(__file__).resolve().parent.parent / "install.sh").read_text()
USER = "me@example.org"
INSTALLER_COMMENTS = (
    "\n# Years of calendar searched before and after this month (1 to 50).\n"
    "# Remove the # and change the number to search further back or ahead.\n"
    "# calendar_years: 10\n"
)


def function(name: str) -> str:
    m = re.search(rf"^# >>> {name}\n(.*?)^# <<< {name}\n", INSTALL, re.S | re.M)
    assert m, f"install.sh lost the markers around {name}"
    return m.group(1)


def bash(script: str, **env: str) -> str:
    out = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", **env},
    )
    return out.stdout


def read_settings(path: Path) -> str:
    return bash(function("read_settings") + 'read_settings "$F"', F=str(path)).strip()


# --- which files the installer counts as its own ---------------------------------------


MANAGED = [
    # (file content, expected "address|contacts|calendars")
    (f'accounts:\n  icloud: {{ preset: icloud, username: "{USER}" }}\n', f"{USER}||"),
    (f"accounts:\n  icloud:\n    preset: icloud\n    username: {USER}\n", f"{USER}||"),
    (f'accounts:\n  icloud:\n    preset: icloud\n    username: "{USER}"\n', f"{USER}||"),
    (
        f'accounts:\n  icloud: {{ preset: icloud, username: "{USER}", contacts: true, calendars: false }}\n',
        f"{USER}|true|false",
    ),
    (
        f'accounts:\n  icloud: {{ preset: icloud, username: "{USER}", contacts: true }}\n'
        + INSTALLER_COMMENTS,
        f"{USER}|true|",
    ),
    (
        f"calendar_years: 10\naccounts:\n  icloud:\n    preset: icloud\n    username: {USER}\n"
        "    contacts: true\n    calendars: true\n",
        f"{USER}|true|true",
    ),
    (f"accounts:\n  icloud: {{ preset: icloud, username: {USER} }}\n\ncalendar_years: 25\n", f"{USER}||"),
    (f"accounts:\r\n  icloud:\r\n    preset: icloud\r\n    username: {USER}\r\n", f"{USER}||"),
]


@pytest.mark.parametrize(("text", "expected"), MANAGED)
def test_settings_this_installer_manages_are_read(tmp_path, text, expected):
    """The block form is the one an op's own file had: the installer called it hand-edited
    and so never offered contacts and calendar."""
    f = tmp_path / "accounts.yaml"
    f.write_text(text, newline="")
    assert read_settings(f) == expected


HAND_EDITED = [
    "",
    "accounts:\n",
    f"accounts:\n  icloud: {{ preset: icloud, username: {USER} }}\n  work: {{ preset: fastmail, username: {USER} }}\n",
    f"accounts:\n  icloud:\n    preset: icloud\n    username: {USER}\n  work:\n    preset: fastmail\n",
    f"accounts:\n  icloud: {{ preset: fastmail, username: {USER} }}\n",
    f"accounts:\n  mine: {{ preset: icloud, username: {USER} }}\n",
    f"accounts:\n  icloud: {{ preset: icloud, username: {USER}, folders: {{ deny: [Junk] }} }}\n",
    f"accounts:\n  icloud:\n    preset: icloud\n    username: {USER}\n    folders:\n      deny: [Junk]\n",
    f"accounts:\n  icloud: {{ preset: icloud, username: {USER}, calendars: {{ allow: [Home] }} }}\n",
    f"accounts:\n  icloud: {{ preset: icloud, username: {USER}, contacts: yes }}\n",
    f"accounts:\n  icloud: {{ preset: icloud, username: {USER}, contacts: true, contacts: false }}\n",
    f"# my mail\naccounts:\n  icloud: {{ preset: icloud, username: {USER} }}\n",
    f"accounts:\n  icloud: {{ preset: icloud, username: {USER} }}\nattachments: deny\n",
    "accounts:\n  icloud: { preset: icloud }\n",
    f"accounts:\n  icloud: {{ preset: icloud, username: {USER} }}\naccounts:\n",
    f"calendar_years: 10\ncalendar_years: 20\naccounts:\n  icloud: {{ preset: icloud, username: {USER} }}\n",
]


@pytest.mark.parametrize("text", HAND_EDITED)
def test_other_settings_are_left_as_hand_edited(tmp_path, text):
    """Anything the installer would lose by rewriting the file (other accounts, folder
    rules, other keys, the user's own comments) keeps the file out of its hands."""
    f = tmp_path / "accounts.yaml"
    f.write_text(text)
    assert read_settings(f) == ""


@pytest.mark.parametrize(("text", "expected"), MANAGED)
def test_every_managed_shape_also_loads(tmp_path, text, expected):
    (tmp_path / "accounts.yaml").write_text(text, newline="")
    acct = load_config(tmp_path, tmp_path / "data").accounts["icloud"]
    assert acct.username == USER
    _, contacts, calendars = expected.split("|")
    assert acct.contacts is (contacts == "true")
    assert (acct.calendars is not None) is (calendars == "true")


# --- writing: the answer is kept, so the question is asked once -------------------------


def write_config(tmp_path: Path, contacts: str, calendars: str, existing: str | None = None) -> Path:
    cfg = tmp_path / "cfg"
    accounts = cfg / "accounts.yaml"
    if existing is not None:
        cfg.mkdir()
        accounts.write_text(existing)
    script = "ok() { :; }\n" + function("write_config") + "write_config"
    bash(
        script,
        CONFIG_DIR=str(cfg),
        ACCOUNTS=str(accounts),
        DATA_DIR=str(tmp_path / "data"),
        EMAIL=USER,
        cur_email=USER if existing is not None else "",
        cur_contacts=contacts,
        cur_calendars=calendars,
        action="sources",
    )
    return accounts


@pytest.mark.parametrize(
    ("contacts", "calendars", "expected"),
    [
        ("", "", "false|false"),
        ("true", "", "true|false"),
        ("", "true", "false|true"),
        ("true", "true", "true|true"),
    ],
)
def test_written_settings_record_both_answers(tmp_path, contacts, calendars, expected):
    """Both flags are written, false included, so the next run knows the question was
    answered: read back, neither key is missing."""
    accounts = write_config(tmp_path, contacts, calendars)
    assert read_settings(accounts) == f"{USER}|{expected}"
    acct = load_config(accounts.parent, tmp_path / "data").accounts["icloud"]
    assert acct.contacts is (contacts == "true")
    assert (acct.calendars is not None) is (calendars == "true")
    assert accounts.stat().st_mode & 0o077 == 0


def test_block_form_is_rewritten_with_the_answers_and_keeps_calendar_years(tmp_path):
    existing = f"calendar_years: 25\naccounts:\n  icloud:\n    preset: icloud\n    username: {USER}\n"
    accounts = write_config(tmp_path, "true", "true", existing)
    assert read_settings(accounts) == f"{USER}|true|true"
    assert load_config(accounts.parent, tmp_path / "data").calendar_years == 25


def test_question_is_asked_until_both_answers_are_written(tmp_path):
    """The installer asks when either key is absent (install.sh sets sources_asked only
    when both are present); after write_config both are present."""
    accounts = tmp_path / "accounts.yaml"
    accounts.write_text(f"accounts:\n  icloud:\n    preset: icloud\n    username: {USER}\n")
    _, contacts, calendars = read_settings(accounts).split("|")
    assert "" in (contacts, calendars)
    _, contacts, calendars = read_settings(write_config(tmp_path, "", "")).split("|")
    assert "" not in (contacts, calendars)


# --- the closing message: first download or catching up ---------------------------------


def mail_downloaded(data: Path) -> bool:
    script = function("mail_downloaded") + "mail_downloaded && echo yes || echo no"
    return bash(script, DATA_DIR=str(data)).strip() == "yes"


def test_first_download_only_when_no_mail_index_exists(tmp_path):
    data = tmp_path / "Application Support" / "readonly-safe-mail-mcp"
    assert not mail_downloaded(data)
    data.mkdir(parents=True)
    assert not mail_downloaded(data)
    (data / "dav-0123456789abcdef.sqlite").write_bytes(b"")
    assert not mail_downloaded(data)
    (data / "index-0123456789abcdef.sqlite").write_bytes(b"")
    assert mail_downloaded(data)
