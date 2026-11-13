"""Contacts sync: CardDAV (read-only, dav.py) -> field policy -> the second index file
(issue #1, "Sync", the Fetch half).

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

from . import credentials, davindex
from .config import Account, Config
from .dav import MAX_MULTIGET_HREFS, DavAuthError, DavError, DavItem, DavLocation, DavSource
from .fieldpolicy import ContactFields, contact_fields, display_line
from .sync import sync_lock
from .vcal import parse_vcard

log = logging.getLogger(__name__)

LOCK_NAME = "dav-sync.lock"
SourceOpener = Callable[[Account, str], DavSource]


class Incomplete(DavError):
    """An answer that cannot be trusted to be the whole collection."""


@dataclass
class DavReport:
    account: str
    source: str = davindex.CONTACTS
    added: int = 0
    removed: int = 0
    error: str | None = None


def open_dav_source(acct: Account, password: str) -> DavSource:
    if acct.dav is None:
        raise DavError("this account has no DAV hosts")
    return DavSource(acct.dav, acct.username, password)


def _fields(text: str, acct: Account) -> ContactFields | None:
    try:
        return contact_fields(parse_vcard(text), acct.rules)
    except Exception:  # fail closed: a card the parser or policy cannot handle keeps nothing
        log.warning("%s: one contact card could not be read; nothing of it is kept", acct.name)
        return None


def _listing(items: list[DavItem]) -> dict[str, str]:
    """{href: etag}, or Incomplete unless every entry has status 200 and an etag."""
    out = {}
    for it in items:
        if it.status != 200 or not it.etag:
            raise Incomplete("address book listing incomplete (an entry without an etag or status 200)")
        out[it.href] = it.etag
    return out


def _sync_book(
    db, src: DavSource, acct: Account, book: davindex.Collection, ctag: str | None, report
) -> None:
    loc = DavLocation(book.host, book.href)
    current = _listing(src.etags(loc))
    known = davindex.contact_etags(db, book.id)
    gone = set(known) - set(current)
    changed = [h for h, e in current.items() if known.get(h) != e]
    for i in range(0, len(changed), MAX_MULTIGET_HREFS):
        batch = changed[i : i + MAX_MULTIGET_HREFS]
        wanted = {unquote(h): h for h in batch}
        answered = set()
        for it in src.multiget("contacts", loc, batch):
            href = wanted.get(unquote(it.href))
            if href is None:
                raise Incomplete("address book answer names a card that was not asked for")
            answered.add(href)
            if it.status == 404:
                gone.add(href)
            elif it.status != 200 or it.data is None or not it.etag:
                raise Incomplete("address book answer incomplete (a card without data or an etag)")
            else:
                davindex.store_contact(db, book.id, href, it.etag, _fields(it.data, acct))
                report.added += 1
        if len(answered) != len(batch):
            raise Incomplete(f"address book answer is missing {len(batch) - len(answered)} card(s)")
    report.removed += davindex.delete_contacts(db, book.id, gone)
    davindex.set_ctag(db, book.id, ctag)


def _sync_contacts(db, src: DavSource, acct: Account, report: DavReport) -> list[str]:
    """Sync every address book of one account. Returns a problem per book left incomplete."""
    found = src.collections(src.home("contacts"))
    if any(it.status != 200 for it in found):
        raise Incomplete("address book discovery incomplete")
    books = [it for it in found if "addressbook" in it.resource_types]
    removed = davindex.drop_collections_except(
        db, acct.name, davindex.CONTACTS, {(b.host, b.href) for b in books}
    )
    if removed:
        log.info("%s: %d address book(s) no longer on the server", acct.name, removed)
    db.commit()
    problems = []
    for it in books:
        book = davindex.save_collection(
            db, acct.name, davindex.CONTACTS, it.host, it.href, display_line(it.displayname or "")
        )
        if it.ctag is None or it.ctag != book.ctag:
            try:
                _sync_book(db, src, acct, book, it.ctag, report)
            except DavAuthError:
                raise
            except DavError as e:
                problems.append(f"{book.name or 'address book'}: {e}")
        # Cards stored before a problem are kept: each row holds what its etag names.
        db.commit()
    return problems


def sync_account(
    cfg: Config,
    acct: Account,
    *,
    open_source: SourceOpener = open_dav_source,
    password_fn: Callable[[str], str] = credentials.get_password,
) -> DavReport:
    report = DavReport(acct.name)
    db = davindex.connect(cfg, create=True)
    assert db is not None
    try:
        password = password_fn(acct.name)
        with open_source(acct, password) as src:
            problems = _sync_contacts(db, src, acct, report)
        report.error = "; ".join(problems) or None
        davindex.record_attempt(db, acct.name, report.source, ok=not problems, error=report.error)
    except Exception as e:  # recorded per account and source; mail and other accounts are not affected
        ours = isinstance(e, (DavError, credentials.CredentialError))
        report.error = (str(e) if ours else "") or type(e).__name__
        if not ours:
            log.warning("%s: contacts sync failed: %s", acct.name, type(e).__name__, exc_info=True)
        db.rollback()
        davindex.record_attempt(db, acct.name, report.source, ok=False, error=report.error)
    finally:
        db.close()
    return report


def sync_all(cfg: Config, *, accounts: list[str] | None = None, **kw) -> list[DavReport]:
    """Sync every account with contacts on, one after another. Call it after sync.sync_all
    has returned. Raises SyncBusy if another DAV sync holds the lock."""
    targets = [a for a in davindex.dav_accounts(cfg) if accounts is None or a.name in accounts]
    if not targets and not davindex.stale_files(cfg):
        return []
    with sync_lock(cfg, LOCK_NAME):
        davindex.remove_other_generations(cfg)
        return [sync_account(cfg, a, **kw) for a in targets]
