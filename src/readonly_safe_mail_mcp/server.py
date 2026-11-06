"""The MCP server (stdio). Tools are read-only views of the local sanitized index.

Each call reloads the config and recomputes the policy fingerprint, so a rule change
takes effect on the next call: the server switches to the index file for the new
policy (empty until resynced) and never serves content indexed under the old rules.
The fingerprint is checked again before returning content.
"""

import logging
import threading
import time

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel

from . import __version__, approval, attachments, index, isolate
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


class SyncRunner:
    """At most one background sync per server process (sync_lock covers other processes)."""

    def __init__(self) -> None:
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

    @staticmethod
    def _run(cfg: Config) -> None:
        try:
            sync_all(cfg)
        except SyncBusy:
            pass
        except Exception:
            log.exception("background sync failed")


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


def build_server() -> MCPServer:
    mcp = MCPServer(
        "readonly-safe-mail",
        version=__version__,
        instructions=(
            "Read-only search over the user's email (local sanitized index). Links and codes are "
            "removed and account-security emails are withheld by design; do not try to recover them. "
            "Workflow: search -> find_passages or read_message on the ids you need. Attachments: "
            "request_attachments with the id and part shown by read_message; the user approves each."
        ),
    )
    runner = SyncRunner()

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
        if runner.running:
            lines.append("A sync is running now.")
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
        notes = await _maybe_sync(cfg, ctx)
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

    async def _maybe_sync(cfg: Config, ctx: Context) -> list[str]:
        """Search-triggered sync policy (PLAN.md section 4.3)."""
        db = _open(cfg)
        try:
            stats = _statuses(cfg, db)
        finally:
            if db:
                db.close()
        last = min((s.last_success or 0) for s in stats) if stats else 0
        age, interval = time.time() - last, cfg.sync_if_older_than
        if age < interval:
            return []
        started = runner.start(cfg)
        # Only the search that starts a sync waits for it. While a long sync (e.g. the initial
        # backfill) is already running, later searches answer immediately from the index.
        if not started or age < 2 * interval:
            return []
        start = time.monotonic()
        told = False
        while runner.running and time.monotonic() - start < MAX_WAIT:
            if not told and time.monotonic() - start >= FIRST_WAIT:
                told = True
                try:
                    await ctx.report_progress(
                        0, None, "Syncing new mail (first search after a break; later searches are instant)…"
                    )
                except Exception:
                    pass
            await anyio.sleep(0.2)
        if runner.running:
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

    return mcp


class AttachmentRef(BaseModel):
    id: int
    part: str


def _text(s: str) -> TextContent:
    return TextContent(type="text", text=s)


def serve() -> None:
    build_server().run("stdio")
