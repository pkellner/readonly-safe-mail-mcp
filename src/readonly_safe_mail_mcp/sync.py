"""Sync: IMAP (read-only) -> sanitizer -> local index (PLAN.md sections 4.2-4.3).

Resumable. A new folder is filled newest-first, so recent mail is searchable within
minutes even when the full mailbox takes hours. Later syncs fetch only UIDs above the
indexed range, continue any unfinished backfill, and drop messages that left the server.
A UIDVALIDITY change discards that folder's rows and starts it again.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field

from . import credentials, index
from .config import Account, Config
from .imap import ImapError, MailSource
from .sanitize import WITHHELD_UNDECODABLE, SanitizedMessage, parse_headers, sanitize_message

log = logging.getLogger(__name__)

CHUNK = 100
ProgressFn = Callable[[str, str, int, int], None]  # account, folder, done, total


class SyncBusy(RuntimeError):
    pass


@dataclass
class SyncReport:
    account: str
    added: int = 0
    removed: int = 0
    error: str | None = None
    folders: list[str] = field(default_factory=list)


@contextmanager
def sync_lock(cfg: Config):
    """At most one sync at a time across processes (CLI and server)."""
    index.ensure_data_dir(cfg.data_dir)
    path = cfg.data_dir / "sync.lock"
    with open(path, "a+") as fh:
        try:
            import fcntl

            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SyncBusy("another sync is already running") from None
        except ImportError:  # Windows: no cross-process lock; WAL still keeps the db consistent
            pass
        yield


def _folder_order(names: list[str]) -> list[str]:
    return sorted(names, key=lambda n: (n.upper() != "INBOX", n.lower()))


def _sanitize(fm, acct: Account) -> SanitizedMessage | None:
    try:
        return sanitize_message(fm, acct.rules)
    except Exception:  # fail closed: never index content the pipeline couldn't handle
        log.warning("%s: sanitizer failed on one message; body withheld", acct.name)
        try:
            headers, _, _ = parse_headers(fm.header_bytes)
        except Exception:
            return None
        return SanitizedMessage(headers, WITHHELD_UNDECODABLE, True, (), ("body withheld",))


def _sync_folder(
    src: MailSource,
    db,
    acct: Account,
    name: str,
    report: SyncReport,
    progress: ProgressFn | None,
    stop: threading.Event | None,
) -> None:
    info = src.examine(name)
    st = index.folder_state(db, acct.name, name)
    if st is None or st.uidvalidity != info.uidvalidity:
        index.reset_folder(db, acct.name, name, info.uidvalidity)
        st = index.FolderState(info.uidvalidity, None, None, False)
    uids = src.search_uids()
    report.removed += index.delete_missing(db, acct.name, name, info.uidvalidity, set(uids))
    if st.high_uid is None:
        new, backfill = [], sorted(uids, reverse=True)
    else:
        new = [u for u in uids if u > st.high_uid]
        backfill = [] if st.complete else sorted((u for u in uids if u < st.low_uid), reverse=True)
    total, done = len(new) + len(backfill), 0
    index.save_folder(db, acct.name, name, st, len(uids))
    db.commit()
    if progress and total:
        progress(acct.name, name, 0, total)

    def run(batch: list[int]) -> None:
        nonlocal done
        for fm in src.fetch_messages(batch):
            sm = _sanitize(fm, acct)
            if sm is not None:
                index.store(db, acct.name, name, info.uidvalidity, fm.uid, fm.size, sm)
                report.added += 1
        st.high_uid = max(batch) if st.high_uid is None else max(st.high_uid, max(batch))
        st.low_uid = min(batch) if st.low_uid is None else min(st.low_uid, min(batch))
        index.save_folder(db, acct.name, name, st, len(uids))
        db.commit()
        done += len(batch)
        if progress:
            progress(acct.name, name, done, total)

    for i in range(0, len(new), CHUNK):  # ascending, extends high_uid upward
        if stop and stop.is_set():
            return
        run(new[i : i + CHUNK])
    for i in range(0, len(backfill), CHUNK):  # descending, extends low_uid downward
        if stop and stop.is_set():
            return
        run(backfill[i : i + CHUNK])
    st.complete = True
    index.save_folder(db, acct.name, name, st, len(uids))
    db.commit()


def sync_account(
    cfg: Config,
    acct: Account,
    *,
    progress: ProgressFn | None = None,
    stop: threading.Event | None = None,
    open_source: Callable[[Account, str], MailSource] | None = None,
    password_fn: Callable[[str], str] = credentials.get_password,
) -> SyncReport:
    report = SyncReport(acct.name)
    db = index.connect(index.index_path(cfg), create=True)
    assert db is not None
    try:
        password = password_fn(acct.name)
        with (open_source or MailSource.open)(acct, password) as src:
            permitted = [n for n, _, ok in src.list_folders() if ok]
            index.drop_folders_except(db, acct.name, permitted)
            db.commit()
            for name in _folder_order(permitted):
                _sync_folder(src, db, acct, name, report, progress, stop)
                report.folders.append(name)
                if stop and stop.is_set():
                    break
        index.record_attempt(db, acct.name, ok=not (stop and stop.is_set()))
    except Exception as e:  # recorded per account; other accounts keep syncing
        ours = isinstance(e, (ImapError, credentials.CredentialError))
        report.error = (str(e) if ours else "") or type(e).__name__
        if not ours:
            log.warning("%s: sync failed: %s", acct.name, type(e).__name__, exc_info=True)
        db.rollback()
        index.record_attempt(db, acct.name, ok=False, error=report.error)
    finally:
        db.close()
    return report


def sync_all(
    cfg: Config,
    *,
    accounts: list[str] | None = None,
    progress: ProgressFn | None = None,
    stop: threading.Event | None = None,
    **kw,
) -> list[SyncReport]:
    """Sync accounts in parallel. Raises SyncBusy if another sync holds the lock."""
    names = accounts or sorted(cfg.accounts)
    with sync_lock(cfg):
        if cfg.accounts:
            index.remove_other_generations(cfg)
        reports: dict[str, SyncReport] = {}

        def work(name: str) -> None:
            reports[name] = sync_account(cfg, cfg.accounts[name], progress=progress, stop=stop, **kw)

        threads = [threading.Thread(target=work, args=(n,), daemon=True) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return [reports[n] for n in names]
