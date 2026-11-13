"""The MCP server (stdio). Tools are read-only views of the local sanitized index.

Each call reloads the config and recomputes the policy fingerprint, so a rule change
takes effect on the next call: the server switches to the index file for the new
policy (empty until resynced) and never serves content indexed under the old rules.
The fingerprint is checked again before returning content.
"""

import logging
import threading
import time
from collections.abc import Callable

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel

from . import __version__, approval, attachments, davindex, davsync, index, isolate
from . import query as q
from .config import Config, ConfigError, load_config
from .imap import DEFAULT_MAX_ATTACHMENT_BYTES
from .sync import SyncBusy, sync_all

log = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
ASKS_USER = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=False)
FIRST_WAIT, MAX_WAIT = 2.0, 10.0
NO_INDEX = (
    "No index yet for the current rules. A background sync has been started; search again in a "
    "minute. For a large mailbox, running `readonly-safe-mail-mcp sync` in a terminal shows progress."
)
POLICY_CHANGED = "Index is rebuilding under updated security rules; search again shortly."
NO_CONTACTS = "Contacts are not turned on for any account (accounts.yaml: contacts: true)."
NO_CONTACTS_INDEX = (
    "Contacts have not been synced yet. A background sync has been started; try again in a minute."
)
NO_CALENDARS = "Calendars are not turned on for any account (accounts.yaml: calendars: true)."
NO_CALENDARS_INDEX = (
    "Calendars have not been synced yet. A background sync has been started; try again in a minute."
)
_SOURCE_TEXT = {  # source -> (off, not synced yet, untrusted-content line)
    davindex.CONTACTS: (NO_CONTACTS, NO_CONTACTS_INDEX, q.UNTRUSTED_CONTACTS),
    davindex.CALENDARS: (NO_CALENDARS, NO_CALENDARS_INDEX, q.UNTRUSTED_EVENTS),
}


class SyncRunner:
    """At most one background sync per server process (sync_lock covers other processes).
    `then` is started when this sync has finished: the DAV sync runs after the mail sync,
    on its own thread, so a slow DAV server never keeps a mail sync from starting."""

    def __init__(self, sync: Callable[[Config], object], then: "SyncRunner | None" = None) -> None:
        self._sync = sync
        self._then = then
        self._thread: threading.Thread | None = None
        self._mu = threading.Lock()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, cfg: Config) -> bool:
        """Start a background sync. False if one is already running."""
        with self._mu:
            if self.running:
                return False
            self._thread = threading.Thread(target=self._run, args=(cfg,), daemon=True)
            self._thread.start()
            return True

    def _run(self, cfg: Config) -> None:
        try:
            self._sync(cfg)
        except SyncBusy:
            pass
        except Exception:
            log.exception("background sync failed")
        if self._then is not None:
            self._then.start(cfg)


def _config() -> Config:
    try:
        return load_config()
    except ConfigError as e:
        raise ToolError(f"config error: {e}") from None


def _open(cfg: Config):
    return index.connect(index.index_path(cfg), create=False)


def _check_policy(fp: str) -> None:
    if index.fingerprint(_config()) != fp:
        raise ToolError(POLICY_CHANGED)


def _statuses(cfg: Config, db) -> list[index.AccountStatus]:
    return index.account_status(db, sorted(cfg.accounts))


def _source_statuses(cfg: Config, source: str) -> list[davindex.SourceStatus]:
    db = davindex.connect(cfg, create=False)
    try:
        return davindex.source_status(db, [a.name for a in davindex.dav_accounts(cfg, source)], source)
    finally:
        if db:
            db.close()


def _sources_on() -> set[str]:
    """At start-up: the DAV sources some account has on. A config that cannot be read leaves
    them off; each tool call reloads the config and reports the error itself."""
    try:
        cfg = load_config()
    except (ConfigError, OSError):
        return set()
    return {s for s in (davindex.CONTACTS, davindex.CALENDARS) if davindex.dav_accounts(cfg, s)}


def build_server() -> MCPServer:
    sources = _sources_on()
    contacts, calendars = davindex.CONTACTS in sources, davindex.CALENDARS in sources
    mcp = MCPServer(
        "readonly-safe-mail",
        version=__version__,
        instructions=(
            "Read-only search over the user's email (local sanitized index). Links and codes are "
            "removed and account-security emails are withheld by design; do not try to recover them. "
            "Workflow: search -> find_passages or read_message on the ids you need. Attachments: "
            "request_attachments with the id and part shown by read_message; the user approves each."
            + (" Contacts: search_contacts -> read_contact on the ids you need." if contacts else "")
            + (" Calendar: search_events -> read_event on the ids you need." if calendars else "")
        ),
    )
    dav_runner = SyncRunner(lambda cfg: davsync.sync_all(cfg))
    runner = SyncRunner(lambda cfg: sync_all(cfg), then=dav_runner)

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    def list_accounts() -> str:
        """Accounts, sync status, and indexed message counts."""
        cfg = _config()
        db = _open(cfg)
        try:
            lines = q.status_lines(_statuses(cfg, db), cfg.sync_if_older_than)
        finally:
            if db:
                db.close()
        statuses = [st for s in _SOURCE_TEXT for st in _source_statuses(cfg, s)]
        lines += q.source_status_lines(statuses, calendar_years=cfg.calendar_years)
        if runner.running:
            lines.append("A sync is running now.")
        words = davindex.source_words(cfg)
        if dav_runner.running and words:
            lines.append(f"A {words} sync is running now.")
        # A DAV error can name an address book or calendar: text the server supplied.
        failed = {st.source for st in statuses if q.failed(st)}
        lines += [_SOURCE_TEXT[s][2] for s in _SOURCE_TEXT if s in failed]
        return "\n".join(lines)

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def search(
        ctx: Context,
        query: str = "",
        from_: str = "",
        to: str = "",
        since: str = "",
        until: str = "",
        account: str = "",
        folder: str = "",
        sort: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """Search email. query: words (all must match), "exact phrase", OR, NOT, prefix*.
        from_/to: substring of name or address. since/until: YYYY-MM-DD. sort: relevance|date.
        One line per hit: #id | account | date | from | subject | snippet."""
        cfg = _config()
        fp = index.fingerprint(cfg)
        notes = await _mail_sync(cfg, ctx)
        db = _open(cfg)
        if db is None:
            return NO_INDEX
        try:
            notes = q.warnings(_statuses(cfg, db), cfg.sync_if_older_than) + notes
            hits = q.search(
                db,
                query=query,
                from_=from_,
                to=to,
                since=since,
                until=until,
                account=account,
                folder=folder,
                sort=sort,
                limit=limit,
                offset=offset,
            )
        except q.QueryError as e:
            raise ToolError(str(e)) from None
        finally:
            db.close()
        _check_policy(fp)
        body = "\n".join(hits) if hits else "No matches."
        return "\n".join([q.UNTRUSTED, *notes, body])

    async def _maybe_sync(sync: SyncRunner, cfg: Config, ctx: Context, last: float, waiting: str) -> bool:
        """Search-triggered sync policy (PLAN.md section 4.3), for mail and for contacts and
        calendars: a sync starts when `last` is older than the interval, and the call that
        starts one after a break of twice the interval waits for it, up to MAX_WAIT. True
        when that wait ran out with the sync still running."""
        age, interval = time.time() - last, cfg.sync_if_older_than
        if age < interval:
            return False
        started = sync.start(cfg)
        # Only the search that starts a sync waits for it. While a long sync (e.g. the initial
        # backfill) is already running, later searches answer immediately from the index.
        if not started or age < 2 * interval:
            return False
        start = time.monotonic()
        told = False
        while sync.running and time.monotonic() - start < MAX_WAIT:
            if not told and time.monotonic() - start >= FIRST_WAIT:
                told = True
                try:
                    await ctx.report_progress(0, None, waiting)
                except Exception:
                    pass
            await anyio.sleep(0.2)
        return sync.running

    async def _mail_sync(cfg: Config, ctx: Context) -> list[str]:
        db = _open(cfg)
        try:
            stats = _statuses(cfg, db)
        finally:
            if db:
                db.close()
        last = min((s.last_success or 0) for s in stats) if stats else 0
        waiting = "Syncing new mail (first search after a break; later searches are instant)…"
        if await _maybe_sync(runner, cfg, ctx, last, waiting):
            return ["Note: continuing with unsynced data; newest mail will appear in your next search."]
        return []

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    def find_passages(ids: list[int], query: str) -> str:
        """Only the paragraphs of the given messages that mention query's words."""
        return _content(lambda db: q.find_passages(db, ids, query))

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    def read_message(id: int) -> str:
        """Full sanitized text of one message (quoted replies removed)."""
        return _content(lambda db: [q.read_message(db, id)])

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    def get_thread(id: int) -> str:
        """The conversation containing a message, oldest first, de-duplicated."""
        return _content(lambda db: q.get_thread(db, id), sep="\n\n---\n\n")

    @mcp.tool(annotations=ASKS_USER, structured_output=False)
    async def request_attachments(
        ctx: Context, items: list[AttachmentRef]
    ) -> list[TextContent | ImageContent]:
        """Open attachments (id + part from read_message). The user approves each in a dialog;
        approved ones are read in a sandbox and returned as sanitized text or images."""
        cfg = _config()
        fp = index.fingerprint(cfg)
        try:
            runner = isolate.find_runner(cfg.attachments)
        except isolate.IsolationUnavailable as e:
            raise ToolError(str(e)) from None
        db = _open(cfg)
        if db is None:
            return [_text(NO_INDEX)]
        try:
            targets, refused = attachments.resolve(
                db, [(i.id, i.part) for i in items], DEFAULT_MAX_ATTACHMENT_BYTES
            )
        finally:
            db.close()
        lines = list(refused)
        if targets:
            cands = [approval.Candidate(t.label, t.kind in ("image", "vcf")) for t in targets]
            if approval.client_name(ctx) in cfg.trusted_elicitation_clients:
                chosen = await approval.ask_elicitation(ctx, cands)
            elif approval.channel_available():
                chosen = await anyio.to_thread.run_sync(approval.ask_native, cands)
            else:
                raise ToolError("no approval dialog is available here, so attachments can't be opened")
            approved = [t for i, t in enumerate(targets) if i in chosen]
            lines += [f"{t.title}: not approved by the user" for t in targets if t not in approved]
            opened = (
                await anyio.to_thread.run_sync(lambda: attachments.open_approved(cfg, approved, runner))
                if approved
                else []
            )
        else:
            opened = []
        _check_policy(fp)
        out: list[TextContent | ImageContent] = [_text(q.UNTRUSTED)]
        for r in opened:
            if r.error:
                lines.append(f"{r.target.title}: {r.error}")
                continue
            note = f" [{'; '.join(r.notes)}]" if r.notes else ""
            out.append(_text(f"=== {r.target.title}{note}\n{r.text}".rstrip()))
            out += [ImageContent(type="image", data=d, mime_type=f"image/{f}") for d, f in r.images]
        if lines:
            out.append(_text("\n".join(lines)))
        return out

    def _content(fn, sep: str = "\n") -> str:
        cfg = _config()
        fp = index.fingerprint(cfg)
        db = _open(cfg)
        if db is None:
            return NO_INDEX
        try:
            out = fn(db)
        except q.QueryError as e:
            raise ToolError(str(e)) from None
        finally:
            db.close()
        _check_policy(fp)
        return q.UNTRUSTED + "\n" + (sep.join(out) if out else "No matching passages.")

    async def _dav(source: str, ctx: Context, fn) -> str:
        off, not_synced, untrusted = _SOURCE_TEXT[source]
        cfg = _config()
        fp = davindex.fingerprint(cfg)
        if fp is None or not davindex.dav_accounts(cfg, source):
            return off
        stats = _source_statuses(cfg, source)
        # Mail's policy, but after a failed attempt the next waits the interval from that
        # attempt, not from the last success: a wrong password is not retried at every call.
        last = max(min(s.last_success or 0 for s in stats), max(s.last_attempt or 0 for s in stats))
        await _maybe_sync(dav_runner, cfg, ctx, last, f"Syncing {davindex.source_words(cfg)}…")
        stats = _source_statuses(cfg, source)
        db = davindex.connect(cfg, create=False)
        if db is None:
            return not_synced
        try:
            out = fn(db, cfg)
        except q.QueryError as e:
            raise ToolError(str(e)) from None
        finally:
            db.close()
        if davindex.fingerprint(_config()) != fp:
            raise ToolError(POLICY_CHANGED)
        notes = q.source_warnings(stats, cfg.sync_if_older_than)
        return "\n".join([untrusted, *notes, *(out or ["No matches."])])

    if contacts:

        @mcp.tool(annotations=READ_ONLY, structured_output=False)
        async def search_contacts(
            ctx: Context,
            query: str = "",
            name: str = "",
            org: str = "",
            email: str = "",
            phone: str = "",
            account: str = "",
            limit: int = 20,
            offset: int = 0,
        ) -> str:
            """Search contacts. query: words in any field (all must match), "exact phrase", OR, NOT,
            prefix*. name/org/email: substring. phone: digits in any format (555 1234 finds (555) 1234).
            One line per hit: #id | account | name | org | email | phone."""
            return await _dav(
                davindex.CONTACTS,
                ctx,
                lambda db, cfg: q.search_contacts(
                    db,
                    query=query,
                    name=name,
                    org=org,
                    email=email,
                    phone=phone,
                    account=account,
                    limit=limit,
                    offset=offset,
                ),
            )

        @mcp.tool(annotations=READ_ONLY, structured_output=False)
        async def read_contact(ctx: Context, id: int) -> str:
            """One contact card (id from search_contacts): phones, emails, addresses, birthday, note."""
            return await _dav(davindex.CONTACTS, ctx, lambda db, cfg: [q.read_contact(db, id)])

    if calendars:

        @mcp.tool(annotations=READ_ONLY, structured_output=False)
        async def search_events(
            ctx: Context,
            query: str = "",
            since: str = "",
            until: str = "",
            account: str = "",
            calendar: str = "",
            limit: int = 20,
            offset: int = 0,
        ) -> str:
            """Search calendar events by date range and/or words. since/until: YYYY-MM-DD, inclusive
            (neither and no query: from today on). query: words in title, location, description or
            people (all must match), "exact phrase", OR, NOT, prefix*. calendar: substring of its name.
            One line per occurrence: #id | account | when | title | location."""
            return await _dav(
                davindex.CALENDARS,
                ctx,
                lambda db, cfg: q.search_events(
                    db,
                    query=query,
                    since=since,
                    until=until,
                    account=account,
                    calendar=calendar,
                    limit=limit,
                    offset=offset,
                    calendar_years=cfg.calendar_years,
                ),
            )

        @mcp.tool(annotations=READ_ONLY, structured_output=False)
        async def read_event(ctx: Context, id: int) -> str:
            """One event (id from search_events): time, repeat rule, location, people, description."""
            return await _dav(davindex.CALENDARS, ctx, lambda db, cfg: [q.read_event(db, id)])

    return mcp


class AttachmentRef(BaseModel):
    id: int
    part: str


def _text(s: str) -> TextContent:
    return TextContent(type="text", text=s)


def serve() -> None:
    build_server().run("stdio")
