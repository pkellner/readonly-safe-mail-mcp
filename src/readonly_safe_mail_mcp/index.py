"""The local sanitized index (PLAN.md section 4.2).

SQLite + FTS5. Only sanitizer output is ever written here: no raw messages, HTML,
links, codes, withheld bodies, or attachment contents.

Policy generations: the database file is named by a fingerprint of everything that
decides what gets stored (effective rules, folders, sanitizer version, schema). A rule
change therefore selects a NEW, empty file; content indexed under the old rules is
never served again, and sync deletes the old files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .sanitize import SANITIZER_VERSION, SanitizedMessage

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
  account TEXT PRIMARY KEY,
  last_attempt REAL, last_success REAL, last_error TEXT, last_error_at REAL
);
CREATE TABLE IF NOT EXISTS folders (
  account TEXT NOT NULL, folder TEXT NOT NULL,
  uidvalidity INTEGER NOT NULL,
  low_uid INTEGER, high_uid INTEGER,   -- every server UID in [low, high] is indexed
  complete INTEGER NOT NULL DEFAULT 0, -- backfill reached the oldest message
  server_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (account, folder)
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity INTEGER NOT NULL, uid INTEGER NOT NULL,
  ts REAL, date TEXT, from_ TEXT, from_addr TEXT, to_ TEXT, cc TEXT, subject TEXT,
  body TEXT, withheld INTEGER NOT NULL, notes TEXT, attachments TEXT, size INTEGER,
  message_id TEXT, in_reply_to TEXT, refs TEXT,
  UNIQUE (account, folder, uidvalidity, uid)
);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS messages_msgid ON messages(message_id);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  subject, from_, to_, body, content='messages', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, subject, from_, to_, body)
  VALUES (new.id, new.subject, new.from_, new.to_, CASE WHEN new.withheld THEN '' ELSE new.body END);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, subject, from_, to_, body)
  VALUES ('delete', old.id, old.subject, old.from_, old.to_,
          CASE WHEN old.withheld THEN '' ELSE old.body END);
END;
"""


def fingerprint(cfg: Config) -> str:
    accounts = {
        name: {
            "host": a.host,
            "port": a.port,
            "username": a.username,
            "folders": [list(a.folders.allow), list(a.folders.deny)],
            "rules": a.rules.as_dict(),
        }
        for name, a in sorted(cfg.accounts.items())
    }
    return fingerprint_of({"schema": SCHEMA_VERSION, "sanitizer": SANITIZER_VERSION, "accounts": accounts})


def fingerprint_of(policy: dict) -> str:
    """The generation name for a policy: a change to anything in `policy` selects a new file."""
    return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()[:16]


def index_path(cfg: Config) -> Path:
    return cfg.data_dir / f"index-{fingerprint(cfg)}.sqlite"


def ensure_data_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def remove_other_generations(cfg: Config) -> list[str]:
    """Delete index files built under any other policy. Returns removed names."""
    return remove_generations(cfg.data_dir, "index", index_path(cfg))


def generation_files(data_dir: Path, prefix: str) -> list[Path]:
    """Every file of every generation named `<prefix>-<fingerprint>.sqlite`."""
    pattern = re.compile(rf"^{re.escape(prefix)}-[0-9a-f]{{16}}\.sqlite(?:-wal|-shm|-journal)?$")
    return [p for p in data_dir.glob(f"{prefix}-*.sqlite*") if pattern.match(p.name)]


def remove_generations(data_dir: Path, prefix: str, keep: Path | None) -> list[str]:
    """Delete the files of every `prefix` generation but `keep` (all of them if None)."""
    removed = []
    for p in generation_files(data_dir, prefix):
        if keep is None or not p.name.startswith(keep.name):
            p.unlink(missing_ok=True)
            removed.append(p.name)
    return removed


def connect(path: Path, *, create: bool, schema: str = SCHEMA) -> sqlite3.Connection | None:
    """Open an index file. With create=False, returns None if it doesn't exist yet."""
    if not create and not path.exists():
        return None
    if create:
        ensure_data_dir(path.parent)
    old = os.umask(0o077)  # owner-only for the db and its -wal/-shm files
    try:
        db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        if create:
            db.executescript(schema)
            db.execute("INSERT OR IGNORE INTO meta VALUES ('created', ?)", (str(time.time()),))
            db.commit()
    finally:
        os.umask(old)
    for p in path.parent.glob(path.name + "*"):
        os.chmod(p, 0o600)
    return db


# --- writes (sync) ------------------------------------------------------------


@dataclass
class FolderState:
    uidvalidity: int
    low_uid: int | None
    high_uid: int | None
    complete: bool


def folder_state(db: sqlite3.Connection, account: str, folder: str) -> FolderState | None:
    r = db.execute(
        "SELECT uidvalidity, low_uid, high_uid, complete FROM folders WHERE account=? AND folder=?",
        (account, folder),
    ).fetchone()
    return FolderState(r[0], r[1], r[2], bool(r[3])) if r else None


def reset_folder(db: sqlite3.Connection, account: str, folder: str, uidvalidity: int) -> None:
    db.execute("DELETE FROM messages WHERE account=? AND folder=?", (account, folder))
    db.execute(
        "INSERT OR REPLACE INTO folders (account, folder, uidvalidity, low_uid, high_uid, complete) "
        "VALUES (?, ?, ?, NULL, NULL, 0)",
        (account, folder, uidvalidity),
    )


def save_folder(db: sqlite3.Connection, account: str, folder: str, st: FolderState, count: int) -> None:
    db.execute(
        "INSERT OR REPLACE INTO folders VALUES (?, ?, ?, ?, ?, ?, ?)",
        (account, folder, st.uidvalidity, st.low_uid, st.high_uid, int(st.complete), count),
    )


def drop_folders_except(db: sqlite3.Connection, account: str, keep: Iterable[str]) -> None:
    keep = list(keep)
    marks = ",".join("?" * len(keep)) or "''"
    db.execute(f"DELETE FROM messages WHERE account=? AND folder NOT IN ({marks})", (account, *keep))
    db.execute(f"DELETE FROM folders WHERE account=? AND folder NOT IN ({marks})", (account, *keep))


def add_pending_folders(db: sqlite3.Connection, account: str, names: Iterable[str]) -> None:
    """Record folders not started yet, so status doesn't report a partial sync as complete.
    uidvalidity 0 never matches the server, so the first sync of the folder resets it."""
    db.executemany(
        "INSERT OR IGNORE INTO folders (account, folder, uidvalidity) VALUES (?, ?, 0)",
        [(account, n) for n in names],
    )


def delete_missing(
    db: sqlite3.Connection, account: str, folder: str, uidvalidity: int, present: set[int]
) -> int:
    """Remove indexed messages that are no longer on the server."""
    rows = db.execute(
        "SELECT id, uid FROM messages WHERE account=? AND folder=? AND uidvalidity=?",
        (account, folder, uidvalidity),
    ).fetchall()
    gone = [r[0] for r in rows if r[1] not in present]
    for i in range(0, len(gone), 500):
        chunk = gone[i : i + 500]
        db.execute(f"DELETE FROM messages WHERE id IN ({','.join('?' * len(chunk))})", chunk)
    return len(gone)


def store(
    db: sqlite3.Connection,
    account: str,
    folder: str,
    uidvalidity: int,
    uid: int,
    size: int,
    sm: SanitizedMessage,
) -> None:
    h = sm.headers
    atts = json.dumps(
        [
            {"part": a.part_id, "name": a.filename, "type": a.content_type, "size": a.size}
            for a in sm.attachments
        ]
    )
    db.execute(
        "DELETE FROM messages WHERE account=? AND folder=? AND uidvalidity=? AND uid=?",
        (account, folder, uidvalidity, uid),
    )
    db.execute(
        "INSERT INTO messages (account, folder, uidvalidity, uid, ts, date, from_, from_addr, to_, cc, "
        "subject, body, withheld, notes, attachments, size, message_id, in_reply_to, refs) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            account,
            folder,
            uidvalidity,
            uid,
            h.timestamp,
            h.date,
            h.from_,
            h.from_addr,
            h.to,
            h.cc,
            h.subject,
            sm.body,
            int(sm.withheld),
            "; ".join(sm.notes),
            atts,
            size,
            h.message_id,
            h.in_reply_to,
            h.references,
        ),
    )


def record_attempt(db: sqlite3.Connection, account: str, *, ok: bool, error: str | None = None) -> None:
    now = time.time()
    db.execute("INSERT OR IGNORE INTO accounts (account) VALUES (?)", (account,))
    if ok:
        db.execute(
            "UPDATE accounts SET last_attempt=?, last_success=?, last_error=NULL WHERE account=?",
            (now, now, account),
        )
    else:
        db.execute(
            "UPDATE accounts SET last_attempt=?, last_error=?, last_error_at=? WHERE account=?",
            (now, error, now, account),
        )
    db.commit()


# --- reads --------------------------------------------------------------------


@dataclass
class AccountStatus:
    account: str
    messages: int
    server_messages: int
    folders: int
    complete: bool
    last_success: float | None
    last_error: str | None
    last_error_at: float | None


def account_status(db: sqlite3.Connection | None, accounts: Iterable[str]) -> list[AccountStatus]:
    out = []
    for name in accounts:
        if db is None:
            out.append(AccountStatus(name, 0, 0, 0, False, None, None, None))
            continue
        n = db.execute("SELECT count(*) FROM messages WHERE account=?", (name,)).fetchone()[0]
        f = db.execute(
            "SELECT count(*), coalesce(sum(server_count),0), coalesce(min(complete),0) "
            "FROM folders WHERE account=?",
            (name,),
        ).fetchone()
        a = db.execute(
            "SELECT last_success, last_error, last_error_at FROM accounts WHERE account=?", (name,)
        ).fetchone()
        out.append(
            AccountStatus(
                name,
                n,
                f[1],
                f[0],
                bool(f[0]) and bool(f[2]),
                a[0] if a else None,
                a[1] if a else None,
                a[2] if a else None,
            )
        )
    return out
