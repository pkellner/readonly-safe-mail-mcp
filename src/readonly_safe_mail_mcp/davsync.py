"""Contacts and calendar sync: CardDAV and CalDAV (read-only, dav.py) -> field policy and
validated timing -> the second index file (issue #1, "Sync"), then the calendar expansion
pass (caltime.py, the "Rolling window" half).

Runs after the mail sync has released its lock, under a lock of its own, so DAV input can
never hold up mail. A DAV failure is recorded for that account's source and never reaches
the mail sync.

Deleting is the dangerous step: a short or broken answer must not look like "these were
removed". So a collection's rows go only after a complete, validated discovery that no
longer lists it, and an item's row only after a complete listing of its collection and
every changed item processed; the new ctag is stored in that same transaction. Anything
less deletes nothing, keeps the old ctag, and the next sync tries again.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import unquote

from . import caltime, credentials, davindex
from .config import Account, Config
from .dav import (
    MAX_MULTIGET_HREFS,
    DavAuthError,
    DavError,
    DavIncomplete,
    DavItem,
    DavLocation,
    DavSource,
    DavTooLarge,
)
from .fieldpolicy import contact_fields, display_line
from .sync import ProgressFn, sync_lock
from .vcal import parse_vcard

log = logging.getLogger(__name__)

LOCK_NAME = "dav-sync.lock"
SourceOpener = Callable[[Account, str], DavSource]


@dataclass
class DavReport:
    account: str
    source: str = davindex.CONTACTS
    added: int = 0
    removed: int = 0
    error: str | None = None
    pending: int = 0  # calendars: events whose occurrences wait for the next sync


def open_dav_source(acct: Account, password: str) -> DavSource:
    if acct.dav is None:
        raise DavError("this account has no DAV hosts")
    return DavSource(acct.dav, acct.username, password)


def _contact(db, coll: int, href: str, etag: str, text: str | None, acct: Account) -> None:
    """`text` None: a card over the size cap, stored unreadable."""
    fields = None
    if text is not None:
        try:
            fields = contact_fields(parse_vcard(text), acct.rules)
        except Exception:  # fail closed: a card the parser or policy cannot handle keeps nothing
            log.warning("%s: one contact card could not be read; nothing of it is kept", acct.name)
    davindex.store_contact(db, coll, href, etag, fields)


def _calendar_object(db, coll: int, href: str, etag: str, text: str | None, acct: Account) -> None:
    """`text` None: an object over the size cap, stored unreadable."""
    events = None
    if text is not None:
        try:
            events = caltime.parse_object(text, acct)
        except Exception:  # fail closed: an object the parser or policy cannot handle keeps nothing
            log.warning("%s: one calendar item could not be read; nothing of it is kept", acct.name)
    davindex.store_object(db, coll, href, etag, events)


@dataclass(frozen=True)
class _Kind:
    """What differs between address books and calendars; the sync itself is shared."""

    source: str
    collection: str  # "address book", "calendar"
    item: str  # "card", "calendar item"
    etags: Callable
    store: Callable
    delete: Callable


_KINDS = {
    davindex.CONTACTS: _Kind(
        davindex.CONTACTS, "address book", "card", davindex.contact_etags, _contact, davindex.delete_contacts
    ),
    davindex.CALENDARS: _Kind(
        davindex.CALENDARS,
        "calendar",
        "calendar item",
        davindex.object_etags,
        _calendar_object,
        davindex.delete_objects,
    ),
}


def _listing(items: list[DavItem], kind: _Kind = _KINDS[davindex.CONTACTS]) -> dict[str, str]:
    """{href: etag}, or DavIncomplete unless every entry has status 200 and an etag."""
    out = {}
    for it in items:
        if it.status != 200 or not it.etag:
            raise DavIncomplete(
                f"{kind.collection} listing incomplete (an entry without an etag or status 200)"
            )
        out[it.href] = it.etag
    return out


def _sync_collection(
    db,
    src: DavSource,
    acct: Account,
    kind: _Kind,
    coll: davindex.Collection,
    ctag: str | None,
    report,
    progress: ProgressFn | None,
) -> None:
    """One address book or calendar. Rows whose href is gone are deleted, and the new ctag
    stored, only after a complete listing and every changed item processed; the caller
    commits. Anything less raises and the caller rolls the deletions back. progress gets
    the changed items fetched so far, under "contacts/<book>" or "calendars/<calendar>"."""
    loc = DavLocation(coll.host, coll.href)
    current = _listing(src.etags(loc), kind)
    known = kind.etags(db, coll.id)
    gone = set(known) - set(current)
    changed = [h for h, e in current.items() if known.get(h) != e]
    batches = [changed[i : i + MAX_MULTIGET_HREFS] for i in range(0, len(changed), MAX_MULTIGET_HREFS)]
    label, total, done = f"{kind.source}/{coll.name or kind.collection}", len(changed), 0
    if progress and total:
        progress(acct.name, label, 0, total)
    while batches:
        batch = batches.pop(0)
        try:
            answer = src.multiget(kind.source, loc, batch)
        except DavTooLarge:
            # Halve until each answer fits; one item over the cap alone is stored unreadable
            # (counted in status, not fetched again until it changes).
            if len(batch) > 1:
                batches[:0] = [batch[: len(batch) // 2], batch[len(batch) // 2 :]]
                continue
            log.warning("%s: one %s is over the size cap; nothing of it is kept", acct.name, kind.item)
            kind.store(db, coll.id, batch[0], current[batch[0]], None, acct)
            done += 1
            if progress:
                progress(acct.name, label, done, total)
            continue
        wanted = {unquote(h): h for h in batch}
        answered = set()
        for it in answer:
            href = wanted.get(unquote(it.href))
            if href is None:
                raise DavIncomplete(f"{kind.collection} answer names a {kind.item} that was not asked for")
            answered.add(href)
            if it.status == 404:
                gone.add(href)
            elif it.status != 200 or it.data is None or not it.etag:
                raise DavIncomplete(
                    f"{kind.collection} answer incomplete (a {kind.item} without data or an etag)"
                )
            else:
                kind.store(db, coll.id, href, it.etag, it.data, acct)
                report.added += 1
        if len(answered) != len(batch):
            raise DavIncomplete(
                f"{kind.collection} answer is missing {len(batch) - len(answered)} {kind.item}(s)"
            )
        done += len(batch)
        if progress:
            progress(acct.name, label, done, total)
    report.removed += kind.delete(db, coll.id, gone)
    davindex.set_ctag(db, coll.id, ctag)


def _wanted(acct: Account, kind: _Kind, it: DavItem) -> bool:
    types = it.resource_types or ()
    if kind.source == davindex.CONTACTS:
        return "addressbook" in types
    if "calendar" not in types or (it.components and "VEVENT" not in it.components):
        return False
    assert acct.calendars is not None
    return acct.calendars.permits_name(it.displayname or "")


def wanted_collections(acct: Account, source: str, found: list[DavItem]) -> list[DavItem]:
    """The address books or calendars of `found` that this account's sync would use."""
    return [it for it in found if _wanted(acct, _KINDS[source], it)]


def _sync_source(
    db, src: DavSource, acct: Account, kind: _Kind, report: DavReport, progress: ProgressFn | None
) -> list[str]:
    """Sync every address book, or every calendar, of one account. Returns a problem per
    collection left incomplete."""
    found = src.collections(src.home(kind.source))
    if any(it.status != 200 for it in found):
        raise DavIncomplete(f"{kind.collection} discovery incomplete")
    stored = davindex.collections(db, acct.name, kind.source)
    if any(it.resource_types is None and any(_same(acct, c, it) for c in stored) for it in found):
        raise DavIncomplete(
            f"{kind.collection} discovery incomplete (a known {kind.collection} without its type)"
        )
    colls = wanted_collections(acct, kind.source, found)
    here = {(c.host, c.href) for c in stored}
    for it in colls:  # now on another partition host: the same collection, re-keyed with its rows
        old = next((c for c in stored if c.host != it.host and _same(acct, c, it)), None)
        if old is not None and (it.host, it.href) not in here:
            davindex.move_collection(db, old.id, it.host)
            here.add((it.host, it.href))
    removed = davindex.drop_collections_except(db, acct.name, kind.source, {(c.host, c.href) for c in colls})
    if removed:
        log.info("%s: %d %s(s) no longer on the server or not wanted", acct.name, removed, kind.collection)
    db.commit()
    problems = []
    for it in colls:
        coll = davindex.save_collection(
            db, acct.name, kind.source, it.host, it.href, display_line(it.displayname or "")
        )
        if it.ctag is None or it.ctag != coll.ctag:
            try:
                _sync_collection(db, src, acct, kind, coll, it.ctag, report, progress)
            except DavAuthError:
                raise
            except DavError as e:
                problems.append(f"{coll.name or kind.collection}: {e}")
        # Items stored before a problem are kept: each row holds what its etag names.
        db.commit()
    return problems


def _same(acct: Account, c: davindex.Collection, it: DavItem) -> bool:
    """Stored collection `c` is the one `it` names: the same href on the same host, or on
    another partition host of the provider (iCloud moves an account between pNN- hosts)."""
    assert acct.dav is not None
    return c.href == it.href and acct.dav.base(c.host) == acct.dav.base(it.host)


def sources_on(acct: Account) -> list[str]:
    on = ((davindex.CONTACTS, acct.contacts), (davindex.CALENDARS, acct.calendars is not None))
    return [source for source, is_on in on if is_on]


def sync_account(
    cfg: Config,
    acct: Account,
    *,
    open_source: SourceOpener = open_dav_source,
    password_fn: Callable[[str], str] = credentials.get_password,
    progress: ProgressFn | None = None,
) -> list[DavReport]:
    """Fetch every DAV source of one account that is on: one report per source."""
    reports = [DavReport(acct.name, source) for source in sources_on(acct)]
    db = davindex.connect(cfg, create=True)
    assert db is not None
    done: list[DavReport] = []
    try:
        password = password_fn(acct.name)
        with open_source(acct, password) as src:
            for report in reports:
                try:
                    problems = _sync_source(db, src, acct, _KINDS[report.source], report, progress)
                except DavAuthError:
                    raise
                except Exception as e:  # this source only: the account's other source still runs
                    _failed(db, acct, report, e)
                else:
                    report.error = "; ".join(problems) or None
                    davindex.record_attempt(db, acct.name, report.source, ok=not problems, error=report.error)
                done.append(report)
    except Exception as e:  # recorded per account and source; mail and other accounts are not affected
        for report in reports:
            if report not in done:
                _failed(db, acct, report, e)
    finally:
        db.close()
    return reports


def _failed(db, acct: Account, report: DavReport, e: Exception) -> None:
    ours = isinstance(e, (DavError, credentials.CredentialError))
    report.error = (str(e) if ours else "") or type(e).__name__
    if not ours:
        log.warning("%s: %s sync failed: %s", acct.name, report.source, type(e).__name__, exc_info=True)
    db.rollback()
    davindex.record_attempt(db, acct.name, report.source, ok=False, error=report.error)


def _expand(
    cfg: Config, reports: list[DavReport], expansion: caltime.Expansion, on_progress: ProgressFn | None
) -> None:
    """Build occurrences for the current window, with no network request: after a fetch,
    and also when nothing changed on the server or it could not be reached. The count is
    across every account, so on_progress gets no account name."""
    db = davindex.connect(cfg, create=True)
    assert db is not None
    count = (lambda done, total: on_progress("", "event dates", done, total)) if on_progress else None
    try:
        progress = caltime.expand_pending(db, cfg.calendar_years, expansion, count)
        for r in reports:
            if r.source == davindex.CALENDARS:
                r.pending = davindex.count_pending(db, progress.key, r.account)
                if progress.stopped:
                    r.error = "; ".join(x for x in (r.error, progress.stopped) if x)
                    davindex.record_attempt(db, r.account, r.source, ok=False, error=r.error)
        if progress.pending:
            log.info("%d repeating event(s) left for the next sync", progress.pending)
    except Exception as e:
        db.rollback()
        log.warning("calendar expansion failed: %s", type(e).__name__, exc_info=True)
        for r in reports:
            if r.source == davindex.CALENDARS and r.error is None:
                r.error = f"calendar expansion failed ({type(e).__name__})"
                davindex.record_attempt(db, r.account, r.source, ok=False, error=r.error)
    finally:
        db.close()


def sync_all(
    cfg: Config,
    *,
    accounts: list[str] | None = None,
    expansion: caltime.Expansion = caltime.PRODUCTION,
    progress: ProgressFn | None = None,
    **kw,
) -> list[DavReport]:
    """Sync every account with a DAV source on, one after another, then expand calendar
    events. Call it after sync.sync_all has returned. Raises SyncBusy if another DAV sync
    holds the lock."""
    targets = [a for a in davindex.dav_accounts(cfg) if accounts is None or a.name in accounts]
    if not targets and not davindex.stale_files(cfg):
        return []
    with sync_lock(cfg, LOCK_NAME):
        davindex.remove_other_generations(cfg)
        reports = [r for a in targets for r in sync_account(cfg, a, progress=progress, **kw)]
        if davindex.dav_accounts(cfg, davindex.CALENDARS):
            _expand(cfg, reports, expansion, progress)
        return reports
