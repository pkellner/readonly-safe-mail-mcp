"""The second index file: what CardDAV and CalDAV sync store (issue #1, "Second index file").

Beside the mail index, never inside it, so that adding contacts and calendars neither
renames nor rebuilds the mail index. Only field-policy output and validated event timing
(caltime.py) are written here.

Named `dav-<fingerprint>.sqlite` by its own policy: this schema, FIELD_POLICY_VERSION and,
for every account with a DAV source on, its username, DAV hosts, rules and which sources
are on (with the calendars' allow/deny lists). A change to any of them selects a new, empty
file, as for mail (index.py). `calendar_years` is not part of it: changing the window only
rebuilds occurrences, which are derived data. With no source on there is no file at all.
File modes and generations are index.py's.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from . import index
from .config import Account, Config
from .fieldpolicy import FIELD_POLICY_VERSION, ContactFields, Item, phone_digits

if TYPE_CHECKING:
    from .caltime import Built, ParsedEvent

SCHEMA_VERSION = "2"
PREFIX = "dav"
CONTACTS = "contacts"
CALENDARS = "calendars"

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
CREATE TABLE IF NOT EXISTS cal_objects (
  id INTEGER PRIMARY KEY,
  collection INTEGER NOT NULL REFERENCES collections(id),
  href TEXT NOT NULL, etag TEXT NOT NULL,
  unreadable INTEGER NOT NULL DEFAULT 0,  -- the object could not be parsed; nothing of it is kept
  UNIQUE (collection, href)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  object INTEGER NOT NULL REFERENCES cal_objects(id),
  title TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '', description_withheld INTEGER NOT NULL DEFAULT 0,
  organizer TEXT NOT NULL DEFAULT '',  -- JSON [name, address] or ''
  attendees TEXT NOT NULL DEFAULT '[]',  -- JSON list of [name, address, status]
  attendees_omitted INTEGER NOT NULL DEFAULT 0,
  people TEXT NOT NULL DEFAULT '',  -- organizer and attendee names and addresses, for search
  status TEXT NOT NULL DEFAULT '', cancelled INTEGER NOT NULL DEFAULT 0,
  invitation INTEGER NOT NULL DEFAULT 0,  -- an invitation the user has not accepted
  dropped TEXT NOT NULL DEFAULT '[]', notes TEXT NOT NULL DEFAULT '[]',
  timing TEXT NOT NULL,  -- JSON: caltime.event_timing's validated values, never iCalendar text
  incomplete TEXT NOT NULL DEFAULT '',  -- why the timing is not fully understood; '' when it is
  expanded TEXT,  -- the window key its occurrences were built for; any other value: pending
  expand_problem TEXT NOT NULL DEFAULT '',  -- why expansion stopped at the first occurrence
  first_label TEXT NOT NULL DEFAULT '',
  maybe_first INTEGER, maybe_last INTEGER  -- days an incomplete event could touch; NULL: unbounded
);
CREATE INDEX IF NOT EXISTS events_object ON events(object);
CREATE INDEX IF NOT EXISTS events_expanded ON events(expanded);
CREATE TABLE IF NOT EXISTS occurrences (
  id INTEGER PRIMARY KEY,
  event INTEGER NOT NULL REFERENCES events(id),
  start REAL NOT NULL,  -- epoch seconds; an all-day occurrence: local midnight
  first_day INTEGER NOT NULL, last_day INTEGER NOT NULL,  -- local date ordinals, inclusive
  label TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS occurrences_days ON occurrences(first_day, last_day);
CREATE INDEX IF NOT EXISTS occurrences_event ON occurrences(event);
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
  title, location, description, people, content='events', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
  INSERT INTO events_fts(rowid, title, location, description, people)
  VALUES (new.id, new.title, new.location,
          CASE WHEN new.description_withheld THEN '' ELSE new.description END, new.people);
END;
CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
  INSERT INTO events_fts(events_fts, rowid, title, location, description, people)
  VALUES ('delete', old.id, old.title, old.location,
          CASE WHEN old.description_withheld THEN '' ELSE old.description END, old.people);
END;
"""


def dav_accounts(cfg: Config, source: str | None = None) -> list[Account]:
    """Accounts with a DAV source on (`source`: that one), by name."""
    return [
        a
        for _, a in sorted(cfg.accounts.items())
        if (a.contacts and source in (None, CONTACTS))
        or (a.calendars is not None and source in (None, CALENDARS))
    ]


def source_words(cfg: Config) -> str:
    """The sources some account has on, in words: 'contacts', 'calendar' or 'contacts and calendar'."""
    words = [w for s, w in ((CONTACTS, "contacts"), (CALENDARS, "calendar")) if dav_accounts(cfg, s)]
    return " and ".join(words)


def fingerprint(cfg: Config) -> str | None:
    accounts = {
        a.name: {
            "username": a.username,
            "dav": dataclasses.asdict(a.dav) if a.dav else None,
            "rules": a.rules.as_dict(),
            "contacts": a.contacts,
            "calendars": dataclasses.asdict(a.calendars) if a.calendars is not None else None,
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
        delete_objects(
            db, cid, [h for (h,) in db.execute("SELECT href FROM cal_objects WHERE collection=?", (cid,))]
        )
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


def object_etags(db: sqlite3.Connection, collection: int) -> dict[str, str]:
    return dict(db.execute("SELECT href, etag FROM cal_objects WHERE collection=?", (collection,)).fetchall())


def _drop_events(db: sqlite3.Connection, obj: int) -> None:
    db.execute("DELETE FROM occurrences WHERE event IN (SELECT id FROM events WHERE object=?)", (obj,))
    db.execute("DELETE FROM events WHERE object=?", (obj,))


def store_object(
    db: sqlite3.Connection, collection: int, href: str, etag: str, events: list[ParsedEvent] | None
) -> None:
    """Insert or replace one calendar object and its events, whose occurrences are then
    pending. `events` None stores an unreadable object: its etag only."""
    db.execute(
        "INSERT INTO cal_objects (collection, href, etag, unreadable) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (collection, href) DO UPDATE SET etag=excluded.etag, unreadable=excluded.unreadable",
        (collection, href, etag, int(events is None)),
    )
    (obj,) = db.execute(
        "SELECT id FROM cal_objects WHERE collection=? AND href=?", (collection, href)
    ).fetchone()
    _drop_events(db, obj)
    for ev in events or ():
        f = ev.fields
        org = [f.organizer.name, f.organizer.address] if f.organizer else None
        people = [p for p in (f.organizer, *f.attendees) if p is not None]
        db.execute(
            "INSERT INTO events (object, title, location, description, description_withheld, organizer, "
            "attendees, attendees_omitted, people, status, cancelled, invitation, dropped, notes, timing, "
            "incomplete) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                obj,
                f.title,
                f.location,
                f.description,
                int(f.description_withheld),
                json.dumps(org) if org else "",
                json.dumps([[p.name, p.address, p.status] for p in f.attendees]),
                f.attendees_omitted,
                " ".join(x for p in people for x in (p.name, p.address) if x),
                f.status,
                int(f.cancelled),
                int(f.invitation_not_accepted),
                json.dumps(list(f.dropped)),
                json.dumps(list(f.notes)),
                json.dumps(ev.timing),
                "; ".join(ev.timing["incomplete"]),
            ),
        )


def delete_objects(db: sqlite3.Connection, collection: int, hrefs: Iterable[str]) -> int:
    n = 0
    for h in hrefs:
        row = db.execute(
            "SELECT id FROM cal_objects WHERE collection=? AND href=?", (collection, h)
        ).fetchone()
        if row is not None:
            _drop_events(db, row[0])
            n += db.execute("DELETE FROM cal_objects WHERE id=?", (row[0],)).rowcount
    return n


def set_window(db: sqlite3.Connection, years: int, first: date, end: date, key: str) -> None:
    """The window occurrences are being built for. Events built under another key are pending."""
    value = json.dumps({"years": years, "first": first.isoformat(), "end": end.isoformat(), "key": key})
    db.execute(
        "INSERT INTO meta VALUES ('window', ?) ON CONFLICT (key) DO UPDATE SET value=excluded.value", (value,)
    )


def stored_window(db: sqlite3.Connection) -> dict | None:
    row = db.execute("SELECT value FROM meta WHERE key='window'").fetchone()
    return json.loads(row[0]) if row else None


def pending_events(db: sqlite3.Connection, key: str) -> list[tuple[int, str]]:
    return db.execute("SELECT id, timing FROM events WHERE expanded IS NOT ? ORDER BY id", (key,)).fetchall()


def count_pending(db: sqlite3.Connection, key: str, account: str | None = None) -> int:
    sql = "SELECT count(*) FROM events e WHERE e.expanded IS NOT ?"
    args: tuple = (key,)
    if account is not None:
        sql += (
            " AND e.object IN (SELECT o.id FROM cal_objects o JOIN collections c ON c.id = o.collection "
            "WHERE c.account = ?)"
        )
        args += (account,)
    return db.execute(sql, args).fetchone()[0]


def store_occurrences(db: sqlite3.Connection, event: int, built: Built, key: str, problem: str) -> None:
    db.execute("DELETE FROM occurrences WHERE event=?", (event,))
    db.executemany(
        "INSERT INTO occurrences (event, start, first_day, last_day, label) VALUES (?, ?, ?, ?, ?)",
        [(event, o.start, o.first_day, o.last_day, o.label) for o in built.occurrences],
    )
    db.execute(
        "UPDATE events SET expanded=?, expand_problem=?, first_label=?, maybe_first=?, maybe_last=? "
        "WHERE id=?",
        (key, problem, built.first_label, built.maybe_first, built.maybe_last, event),
    )


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
    items: int  # cards, or events
    unreadable: int  # cards, or calendar objects
    collections: int
    last_success: float | None
    last_error: str | None
    last_error_at: float | None
    incomplete: int = 0  # events whose timing is not fully understood
    pending: int = 0  # events whose occurrences are not built for the current window yet
    window: str = ""  # the days occurrences are built for, "2016-11-01 to 2036-11-30"; "" before any


_COUNTS = {
    CONTACTS: (
        "SELECT count(*), coalesce(sum(t.unreadable), 0), 0, 0 FROM contacts t "
        "JOIN collections c ON c.id = t.collection WHERE c.account=? AND c.source=?"
    ),
    CALENDARS: (
        "SELECT (SELECT count(*) FROM events e JOIN cal_objects o ON o.id = e.object "
        "  JOIN collections c ON c.id = o.collection WHERE c.account=?1 AND c.source=?2), "
        "(SELECT count(*) FROM cal_objects o JOIN collections c ON c.id = o.collection "
        "  WHERE o.unreadable AND c.account=?1 AND c.source=?2), "
        "(SELECT count(*) FROM events e JOIN cal_objects o ON o.id = e.object "
        "  JOIN collections c ON c.id = o.collection "
        "  WHERE (e.incomplete != '' OR e.expand_problem != '') AND c.account=?1 AND c.source=?2), "
        "(SELECT count(*) FROM events e JOIN cal_objects o ON o.id = e.object "
        "  JOIN collections c ON c.id = o.collection "
        "  WHERE e.expanded IS NOT ?3 AND c.account=?1 AND c.source=?2)"
    ),
}


def source_status(db: sqlite3.Connection | None, accounts: Iterable[str], source: str) -> list[SourceStatus]:
    out = []
    win = stored_window(db) if db is not None and source == CALENDARS else None
    label = (
        f"{win['first']} to {(date.fromisoformat(win['end']) - timedelta(days=1)).isoformat()}" if win else ""
    )
    for name in accounts:
        if db is None:
            out.append(SourceStatus(name, source, 0, 0, 0, None, None, None))
            continue
        args = (name, source) if source == CONTACTS else (name, source, win["key"] if win else "")
        n, bad, incomplete, pending = db.execute(_COUNTS[source], args).fetchone()
        cols = db.execute(
            "SELECT count(*) FROM collections WHERE account=? AND source=?", (name, source)
        ).fetchone()[0]
        s = db.execute(
            "SELECT last_success, last_error, last_error_at FROM sources WHERE account=? AND source=?",
            (name, source),
        ).fetchone()
        out.append(
            SourceStatus(
                name, source, n, bad, cols, *(s if s else (None, None, None)), incomplete, pending, label
            )
        )
    return out
