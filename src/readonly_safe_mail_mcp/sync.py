"""Sync: IMAP (read-only) -> sanitizer -> local index (PLAN.md sections 4.2-4.3).

Resumable. A new folder is filled newest-first, so recent mail is searchable within
minutes even when the full mailbox takes hours. Later syncs fetch only UIDs above the
indexed range, continue any unfinished backfill, and drop messages that left the server.
A UIDVALIDITY change discards that folder's rows and starts it again.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field

from . import credentials, index
from .config import Account, Config
from .imap import ImapError, MailSource
from .sanitize import WITHHELD_UNDECODABLE, SanitizedMessage, parse_headers, sanitize_message

log = logging.getLogger(__name__)

CHUNK = 100
# iCloud serves message bodies slowly per connection (~10 s per 100 messages) but in
# parallel well: 4 connections measured ~2.8x faster on 2026-11-06. Reduced to 2 after
# iCloud began refusing logins ("[UNAVAILABLE] Service temporarily unavailable") for days
# following a full sync at 4; gentler on the account at some cost in speed.
CONNECTIONS = 2
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
        except ImportError:  # Windows
            import msvcrt

            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise SyncBusy("another sync is already running") from None
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            return
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncBusy("another sync is already running") from None
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


class _Fetchers:
    """A pool of extra read-only connections that fetch batches in parallel. Each thread
    owns one connection. The caller still writes to the index on a single thread."""

    def __init__(self, open_fn: Callable[[], MailSource], n: int) -> None:
        self._open = open_fn
        self._local = threading.local()
        self._sources: list[MailSource] = []
        self._mu = threading.Lock()
        self.window = 2 * n
        self.pool = ThreadPoolExecutor(n, thread_name_prefix="fetch")

    def fetch(self, folder: str, uidvalidity: int, batch: list[int]):
        loc = self._local
        if getattr(loc, "src", None) is None:
            loc.src, loc.folder = self._open(), None
            with self._mu:
                self._sources.append(loc.src)
        if loc.folder != folder:
            if loc.src.examine(folder).uidvalidity != uidvalidity:
                raise ImapError("folder changed on the server during sync; it will restart next sync")
            loc.folder = folder
        return loc.src.fetch_messages(batch)

    def ordered(self, folder: str, uidvalidity: int, batches: list[list[int]], stop):
        """Yield (batch, messages) in submission order, keeping at most `window` in flight."""
        pending: deque = deque()
        it = iter(batches)
        while True:
            while len(pending) < self.window and not (stop and stop.is_set()):
                b = next(it, None)
                if b is None:
                    break
                pending.append((b, self.pool.submit(self.fetch, folder, uidvalidity, b)))
            if not pending:
                return
            b, fut = pending.popleft()
            yield b, fut.result()

    def close(self) -> None:
        self.pool.shutdown(wait=True, cancel_futures=True)
        for src in self._sources:
            src.close()


def _sync_folder(
    src: MailSource,
    fetchers: _Fetchers,
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

    def apply(batch: list[int], fetched) -> None:
        nonlocal done
        for fm in fetched:
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

    # New mail ascending (extends high_uid up), then backfill descending (extends low_uid down).
    # Batches are fetched in parallel but applied strictly in this order, so every UID in
    # [low_uid, high_uid] is indexed whenever state is saved.
    batches = [new[i : i + CHUNK] for i in range(0, len(new), CHUNK)]
    batches += [backfill[i : i + CHUNK] for i in range(0, len(backfill), CHUNK)]
    for batch, fetched in fetchers.ordered(name, info.uidvalidity, batches, stop):
        apply(batch, fetched)
    if stop and stop.is_set():
        return
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
    connections: int = CONNECTIONS,
) -> SyncReport:
    report = SyncReport(acct.name)
    db = index.connect(index.index_path(cfg), create=True)
    assert db is not None
    try:
        password = password_fn(acct.name)
        opener = open_source or MailSource.open
        with opener(acct, password) as src:
            permitted = [n for n, _, ok in src.list_folders() if ok]
        index.drop_folders_except(db, acct.name, permitted)
        index.add_pending_folders(db, acct.name, permitted)
        db.commit()
        fetchers = _Fetchers(lambda: opener(acct, password), max(1, connections))
        try:
            for name in _folder_order(permitted):
                # A fresh control connection per folder: it sits idle while the fetchers
                # work, and iCloud drops idle connections during a long folder.
                with opener(acct, password) as src:
                    _sync_folder(src, fetchers, db, acct, name, report, progress, stop)
                report.folders.append(name)
                if stop and stop.is_set():
                    break
        finally:
            fetchers.close()
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
            while t.is_alive():  # short joins so signals reach the main thread
                t.join(0.5)
        return [reports[n] for n in names]
