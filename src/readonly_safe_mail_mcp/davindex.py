"""The second index file: what CardDAV sync stores (issue #1, "Second index file").

Beside the mail index, never inside it, so that adding contacts (and later calendars)
neither renames nor rebuilds the mail index. Only field-policy output is written here.

Named `dav-<fingerprint>.sqlite` by its own policy: this schema, FIELD_POLICY_VERSION and,
for every account with a DAV source on, its username, DAV hosts, rules and which sources
are on. A change to any of them selects a new, empty file, as for mail (index.py). With no
source on there is no file at all. File modes and generations are index.py's.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from . import index
from .config import Account, Config
from .fieldpolicy import FIELD_POLICY_VERSION, ContactFields, Item, phone_digits

SCHEMA_VERSION = "1"
PREFIX = "dav"
CONTACTS = "contacts"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources (
  account TEXT NOT NULL, source TEXT NOT NULL,
  last_attempt REAL, last_success REAL, last_error TEXT, last_error_at REAL,
  PRIMARY KEY (account, source)
);
CREATE TABLE IF NOT EXISTS collections (
  id INTEGER PRIMARY KEY,
  account TEXT NOT NULL, source TEXT NOT NULL, host TEXT NOT NULL, href TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  ctag TEXT,  -- the server's ctag when every member was last stored; NULL: list it next sync
  UNIQUE (account, source, host, href)
);
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY,
  collection INTEGER NOT NULL REFERENCES collections(id),
  href TEXT NOT NULL, etag TEXT NOT NULL,
  unreadable INTEGER NOT NULL DEFAULT 0,  -- the card could not be parsed; nothing of it is kept
  name TEXT NOT NULL DEFAULT '', org TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
  nickname TEXT NOT NULL DEFAULT '',
  emails TEXT NOT NULL DEFAULT '[]', phones TEXT NOT NULL DEFAULT '[]',
  addresses TEXT NOT NULL DEFAULT '[]',  -- JSON lists of [label, value]
  phone_digits TEXT NOT NULL DEFAULT '',  -- ' 15550100 5551234 ': each phone, digits only
  birthday TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
  note_withheld INTEGER NOT NULL DEFAULT 0,
  dropped TEXT NOT NULL DEFAULT '[]', notes TEXT NOT NULL DEFAULT '[]',
  UNIQUE (collection, href)
);
CREATE INDEX IF NOT EXISTS contacts_name ON contacts(name);
CREATE VIRTUAL TABLE IF NOT EXISTS contacts_fts USING fts5(
  name, org, title, nickname, emails, addresses, note, content='contacts', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS contacts_ai AFTER INSERT ON contacts BEGIN
  INSERT INTO contacts_fts(rowid, name, org, title, nickname, emails, addresses, note)
  VALUES (new.id, new.name, new.org, new.title, new.nickname, new.emails, new.addresses,
          CASE WHEN new.note_withheld THEN '' ELSE new.note END);
END;
CREATE TRIGGER IF NOT EXISTS contacts_ad AFTER DELETE ON contacts BEGIN
  INSERT INTO contacts_fts(contacts_fts, rowid, name, org, title, nickname, emails, addresses, note)
  VALUES ('delete', old.id, old.name, old.org, old.title, old.nickname, old.emails, old.addresses,
          CASE WHEN old.note_withheld THEN '' ELSE old.note END);
END;
CREATE TRIGGER IF NOT EXISTS contacts_au AFTER UPDATE ON contacts BEGIN
  INSERT INTO contacts_fts(contacts_fts, rowid, name, org, title, nickname, emails, addresses, note)
  VALUES ('delete', old.id, old.name, old.org, old.title, old.nickname, old.emails, old.addresses,
          CASE WHEN old.note_withheld THEN '' ELSE old.note END);
  INSERT INTO contacts_fts(rowid, name, org, title, nickname, emails, addresses, note)
  VALUES (new.id, new.name, new.org, new.title, new.nickname, new.emails, new.addresses,
          CASE WHEN new.note_withheld THEN '' ELSE new.note END);
END;
"""


def dav_accounts(cfg: Config) -> list[Account]:
    """Accounts with a DAV source on, by name."""
    return [a for _, a in sorted(cfg.accounts.items()) if a.contacts]


def fingerprint(cfg: Config) -> str | None:
    accounts = {
        a.name: {
            "username": a.username,
            "dav": dataclasses.asdict(a.dav) if a.dav else None,
            "rules": a.rules.as_dict(),
            "contacts": a.contacts,
        }
        for a in dav_accounts(cfg)
    }
    if not accounts:
        return None
    return index.fingerprint_of(
        {"schema": SCHEMA_VERSION, "field_policy": FIELD_POLICY_VERSION, "accounts": accounts}
    )


def index_path(cfg: Config) -> Path | None:
    fp = fingerprint(cfg)
    return cfg.data_dir / f"{PREFIX}-{fp}.sqlite" if fp else None


def connect(cfg: Config, *, create: bool) -> sqlite3.Connection | None:
    """Open the current generation. None if no source is on, or (create=False) no file yet."""
    path = index_path(cfg)
    if path is None:
        return None
    return index.connect(path, create=create, schema=SCHEMA)


def stale_files(cfg: Config) -> bool:
    keep = index_path(cfg)
    return any(
        keep is None or not p.name.startswith(keep.name) for p in index.generation_files(cfg.data_dir, PREFIX)
    )


def remove_other_generations(cfg: Config) -> list[str]:
    """Delete DAV files built under any other policy; every one of them when no source is on."""
    return index.remove_generations(cfg.data_dir, PREFIX, index_path(cfg))


# --- writes (sync) ------------------------------------------------------------


@dataclass(frozen=True)
class Collection:
    id: int
    host: str
    href: str
    name: str
    ctag: str | None


def collections(db: sqlite3.Connection, account: str, source: str) -> list[Collection]:
    rows = db.execute(
        "SELECT id, host, href, name, ctag FROM collections WHERE account=? AND source=? ORDER BY id",
        (account, source),
    ).fetchall()
    return [Collection(*r) for r in rows]


def save_collection(
    db: sqlite3.Connection, account: str, source: str, host: str, href: str, name: str
) -> Collection:
    """The stored row for a discovered collection, created with no ctag if new."""
    db.execute(
        "INSERT INTO collections (account, source, host, href, name) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (account, source, host, href) DO UPDATE SET name=excluded.name",
        (account, source, host, href, name),
    )
    r = db.execute(
        "SELECT id, host, href, name, ctag FROM collections "
        "WHERE account=? AND source=? AND host=? AND href=?",
        (account, source, host, href),
    ).fetchone()
    return Collection(*r)


def drop_collections_except(
    db: sqlite3.Connection, account: str, source: str, keep: Iterable[tuple[str, str]]
) -> int:
    """Delete collections (and their items) that a complete discovery no longer lists."""
    keep = set(keep)
    gone = [c.id for c in collections(db, account, source) if (c.host, c.href) not in keep]
    for cid in gone:
        db.execute("DELETE FROM contacts WHERE collection=?", (cid,))
        db.execute("DELETE FROM collections WHERE id=?", (cid,))
    return len(gone)


def set_ctag(db: sqlite3.Connection, collection: int, ctag: str | None) -> None:
    db.execute("UPDATE collections SET ctag=? WHERE id=?", (ctag, collection))


def contact_etags(db: sqlite3.Connection, collection: int) -> dict[str, str]:
    return dict(db.execute("SELECT href, etag FROM contacts WHERE collection=?", (collection,)).fetchall())


def _items(items: tuple[Item, ...]) -> str:
    return json.dumps([[i.label, i.value] for i in items])


_UNREADABLE = ContactFields("", "", "", "", (), (), (), "", "", False, (), ())
_CONTACT_COLS = (
    "unreadable, name, org, title, nickname, emails, phones, addresses, phone_digits, birthday, note, "
    "note_withheld, dropped, notes"
).split(", ")


def store_contact(
    db: sqlite3.Connection, collection: int, href: str, etag: str, fields: ContactFields | None
) -> None:
    """Insert or update one card. `fields` None stores an unreadable card: its etag only,
    so it is not fetched again until it changes."""
    f = fields or _UNREADABLE
    row = (
        int(fields is None),
        f.name,
        f.org,
        f.title,
        f.nickname,
        _items(f.emails),
        _items(f.phones),
        _items(f.addresses),
        "".join(f" {phone_digits(p.value)}" for p in f.phones) + " ",
        f.birthday,
        f.note,
        int(f.note_withheld),
        json.dumps(list(f.dropped)),
        json.dumps(list(f.notes)),
    )
    db.execute(
        f"INSERT INTO contacts (collection, href, etag, {', '.join(_CONTACT_COLS)}) "
        f"VALUES (?, ?, ?, {', '.join('?' * len(_CONTACT_COLS))}) "
        "ON CONFLICT (collection, href) DO UPDATE SET etag=excluded.etag, "
        + ", ".join(f"{c}=excluded.{c}" for c in _CONTACT_COLS),
        (collection, href, etag, *row),
    )


def delete_contacts(db: sqlite3.Connection, collection: int, hrefs: Iterable[str]) -> int:
    n = 0
    for h in hrefs:
        n += db.execute("DELETE FROM contacts WHERE collection=? AND href=?", (collection, h)).rowcount
    return n


def record_attempt(
    db: sqlite3.Connection, account: str, source: str, *, ok: bool, error: str | None = None
) -> None:
    now = time.time()
    db.execute("INSERT OR IGNORE INTO sources (account, source) VALUES (?, ?)", (account, source))
    if ok:
        db.execute(
            "UPDATE sources SET last_attempt=?, last_success=?, last_error=NULL WHERE account=? AND source=?",
            (now, now, account, source),
        )
    else:
        db.execute(
            "UPDATE sources SET last_attempt=?, last_error=?, last_error_at=? WHERE account=? AND source=?",
            (now, error, now, account, source),
        )
    db.commit()


# --- reads --------------------------------------------------------------------


@dataclass(frozen=True)
class SourceStatus:
    account: str
    source: str
    items: int
    unreadable: int
    collections: int
    last_success: float | None
    last_error: str | None
    last_error_at: float | None


def source_status(db: sqlite3.Connection | None, accounts: Iterable[str], source: str) -> list[SourceStatus]:
    out = []
    for name in accounts:
        if db is None:
            out.append(SourceStatus(name, source, 0, 0, 0, None, None, None))
            continue
        n, bad = db.execute(
            "SELECT count(*), coalesce(sum(t.unreadable), 0) FROM contacts t "
            "JOIN collections c ON c.id = t.collection WHERE c.account=? AND c.source=?",
            (name, source),
        ).fetchone()
        cols = db.execute(
            "SELECT count(*) FROM collections WHERE account=? AND source=?", (name, source)
        ).fetchone()[0]
        s = db.execute(
            "SELECT last_success, last_error, last_error_at FROM sources WHERE account=? AND source=?",
            (name, source),
        ).fetchone()
        out.append(SourceStatus(name, source, n, bad, cols, *(s if s else (None, None, None))))
    return out
