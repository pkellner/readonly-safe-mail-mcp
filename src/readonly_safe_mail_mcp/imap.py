"""Read-only IMAP access (PLAN.md section 4.4).

Two independent layers:

1. MailSource exposes narrow methods that build their own commands. Nothing
   accepts a raw command or fetch expression. Whole-message fetches do not exist.
   Attachment parts can only be fetched with an Approval.

2. Every command passes through check_command() inside the connection object
   itself (_ReadOnlyGuard._command). Anything not on the allowlist raises before
   a byte is sent -- including imaplib methods like store(), copy(), expunge().

TLS is always verified (imaplib's default context does NOT verify certificates).
"""

from __future__ import annotations

import imaplib
import logging
import re
import ssl
import time
from dataclasses import dataclass
from datetime import date

from .config import Account, Folders
from .mime import (
    PART_ID_PATTERN,
    PART_ID_RE,
    FetchedMessage,
    ResponseParseError,
    Structure,
    StructureError,
    parse_bodystructure,
    parse_fetch,
    parse_list,
)

log = logging.getLogger(__name__)

HEADER_FIELDS = ("FROM", "TO", "CC", "DATE", "SUBJECT", "MESSAGE-ID", "IN-REPLY-TO", "REFERENCES")
MAX_HEADER_BYTES = 65_536
DEFAULT_MAX_ATTACHMENT_BYTES = 20_000_000
FETCH_BATCH = 100
DEFAULT_TIMEOUT = 30

HEADER_ITEM = f"(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({' '.join(HEADER_FIELDS)})]<0.{MAX_HEADER_BYTES}>)"
STRUCTURE_ITEM = "(UID BODYSTRUCTURE)"
_PART_ITEM_RE = re.compile(rf"^\(UID BODY\.PEEK\[{PART_ID_PATTERN}\]<0\.\d{{1,10}}>\)$")
_UIDSET_RE = re.compile(rf"^\d{{1,10}}(?:,\d{{1,10}}){{0,{FETCH_BATCH - 1}}}$")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_SEARCH_ARG_RE = re.compile(rf"^(?:ALL|UID|SINCE|\d{{1,10}}:\*|\d{{1,2}}-(?:{'|'.join(_MONTHS)})-\d{{4}})$")
_QUOTED_RE = re.compile(r'^"(?:[^"\\\r\n\x00]|\\["\\])*"$')
_ALWAYS_DENIED_FLAGS = frozenset({"\\junk", "\\trash", "\\drafts", "\\noselect", "\\nonexistent"})


class ImapError(RuntimeError):
    pass


class PolicyError(ImapError):
    """Refused by this server's own read-only or approval policy. Nothing was sent."""


# --- layer 2: the command guard ---------------------------------------------


def check_command(name, args) -> None:
    """Raise PolicyError unless (name, args) is one of the exact command shapes this
    package uses. Error messages never include arguments (LOGIN carries the password)."""
    n = str(name).upper()
    a = [x.decode("ascii", "replace") if isinstance(x, bytes) else str(x) for x in args if x is not None]
    if n in ("CAPABILITY", "NOOP", "LOGOUT") and not a:
        return
    if n == "LOGIN" and len(a) == 2:
        return
    if n == "LIST" and a == ['""', "*"]:
        return
    if n == "EXAMINE" and len(a) == 1 and _QUOTED_RE.match(a[0]):
        return
    if n == "UID" and a:
        sub = a[0].upper()
        if sub == "SEARCH" and len(a) >= 2 and all(_SEARCH_ARG_RE.match(x) for x in a[1:]):
            return
        if sub == "FETCH" and len(a) == 3 and _UIDSET_RE.match(a[1]):
            item = a[2]
            if item in (HEADER_ITEM, STRUCTURE_ITEM) or _PART_ITEM_RE.match(item):
                return
        raise PolicyError(f"IMAP command refused by read-only guard: UID {sub}")
    raise PolicyError(f"IMAP command refused by read-only guard: {n}")


class _ReadOnlyGuard:
    """Mixin for imaplib classes. Every command funnels through imaplib's _command."""

    def _command(self, name, *args):
        check_command(name, args)
        return super()._command(name, *args)


class GuardedIMAP4_SSL(_ReadOnlyGuard, imaplib.IMAP4_SSL):
    pass


def quote_mailbox(name: str) -> str:
    if not name or len(name) > 255 or not name.isascii() or any(c in name for c in "\r\n\x00"):
        raise PolicyError("unsupported folder name")
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _imap_date(d: date) -> str:
    return f"{d.day}-{_MONTHS[d.month - 1]}-{d.year}"  # strftime %b is locale-dependent


# --- layer 1: narrow methods ------------------------------------------------


@dataclass(frozen=True)
class FolderInfo:
    name: str
    uidvalidity: int
    uidnext: int | None
    exists: int


@dataclass(frozen=True)
class HeaderRecord:
    uid: int
    size: int
    header_bytes: bytes


@dataclass(frozen=True)
class Approval:
    """Permission to fetch one attachment part. Created only by the human-approval
    flow (step 5), which also enforces single use."""

    account: str
    folder: str
    uidvalidity: int
    uid: int
    part_id: str
    request_id: str
    expires_at: float  # time.time()

    def expired(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) >= self.expires_at


_MISSING = object()


def default_tls_context() -> ssl.SSLContext:
    """Verify server certificates against the operating system's trust store
    (macOS Keychain, Windows certificate store, Linux system bundle).

    Plain ssl.create_default_context() relies on OpenSSL's CA paths, which are empty
    for python.org builds on macOS, so every connection would fail verification."""
    import truststore

    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class MailSource:
    def __init__(
        self,
        conn: _ReadOnlyGuard,
        *,
        account: str,
        folders: Folders,
        max_part_bytes: int,
        max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    ) -> None:
        if not isinstance(conn, _ReadOnlyGuard):
            raise TypeError("MailSource requires a guarded IMAP connection")
        self._conn = conn
        self.account = account
        self._folders = folders
        self._max_part_bytes = max_part_bytes
        self._max_attachment_bytes = max_attachment_bytes
        self._folder_flags: dict[str, tuple[str, ...]] | None = None
        self._current: FolderInfo | None = None
        self._structures: dict[tuple[str, int, int], Structure] = {}

    # -- connection --

    @classmethod
    def open(
        cls,
        account: Account,
        password: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        ssl_context: ssl.SSLContext | None = None,
    ) -> MailSource:
        ctx = ssl_context or default_tls_context()
        if ctx.verify_mode != ssl.CERT_REQUIRED or not ctx.check_hostname:
            raise PolicyError("TLS certificate and hostname verification must be enabled")
        # The TLS handshake (and certificate check) completes inside the constructor,
        # before LOGIN can be sent.
        conn = GuardedIMAP4_SSL(account.host, account.port, ssl_context=ctx, timeout=timeout)
        src = cls(
            conn,
            account=account.name,
            folders=account.folders,
            max_part_bytes=account.rules.max_message_bytes,
        )
        try:
            src.login(account.username, password)
        except BaseException:
            src.close()
            raise
        return src

    def login(self, username: str, password: str) -> None:
        try:
            self._conn.login(username, password)
        except imaplib.IMAP4.error as e:
            raise ImapError(f"{self.account}: login failed: {e}") from None

    def close(self) -> None:
        try:
            self._conn.logout()
        except Exception:
            pass

    def __enter__(self) -> MailSource:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- folders --

    def list_folders(self) -> list[tuple[str, tuple[str, ...], bool]]:
        """[(name, flags, permitted_by_config)]"""
        typ, data = self._conn.list()
        if typ != "OK":
            raise ImapError(f"{self.account}: LIST failed")
        entries = parse_list(data)
        self._folder_flags = {name: flags for flags, name in entries}
        return [(name, flags, self._permitted(name, flags)) for flags, name in entries]

    def _permitted(self, name: str, flags: tuple[str, ...]) -> bool:
        if {f.lower() for f in flags} & _ALWAYS_DENIED_FLAGS:
            return False
        return self._folders.permits(name)

    def examine(self, name: str) -> FolderInfo:
        if self._folder_flags is None:
            self.list_folders()
        assert self._folder_flags is not None
        if name not in self._folder_flags:
            raise PolicyError("folder is not in the server's folder list")
        if not self._permitted(name, self._folder_flags[name]):
            raise PolicyError("folder is not permitted by configuration")
        typ, data = self._conn.select(quote_mailbox(name), readonly=True)  # EXAMINE, never SELECT
        if typ != "OK":
            raise ImapError(f"{self.account}: could not open folder")
        uidvalidity = self._response_int("UIDVALIDITY")
        if uidvalidity is None:
            raise ImapError(f"{self.account}: server did not report UIDVALIDITY")
        exists = int(data[0]) if data and data[0] and data[0].isdigit() else 0
        self._current = FolderInfo(name, uidvalidity, self._response_int("UIDNEXT"), exists)
        return self._current

    def _response_int(self, code: str) -> int | None:
        _, data = self._conn.response(code)
        v = data[-1] if data else None
        if isinstance(v, bytes) and v.strip().isdigit():
            return int(v)
        return None

    def _folder(self) -> FolderInfo:
        if self._current is None:
            raise ImapError("no folder open")
        return self._current

    # -- search & fetch --

    def search_uids(self, *, min_uid: int | None = None, since: date | None = None) -> list[int]:
        """UIDs in the open folder. Note: imaplib caps response lines at 1 MB, so callers
        with very large folders should search in UID windows (min_uid)."""
        self._folder()
        args: list[str] = []
        if min_uid is not None:
            args += ["UID", f"{int(min_uid)}:*"]
        if since is not None:
            args += ["SINCE", _imap_date(since)]
        typ, data = self._conn.uid("SEARCH", *(args or ["ALL"]))
        if typ != "OK":
            raise ImapError(f"{self.account}: SEARCH failed")
        uids = sorted({int(x) for x in (data[0] or b"").split() if x.isdigit()})
        if min_uid is not None:  # "n:*" always includes the highest UID, even if below n
            uids = [u for u in uids if u >= min_uid]
        return uids

    def _fetch(self, uids: list[int], item: str) -> dict[int, dict[str, object]]:
        typ, data = self._conn.uid("FETCH", ",".join(str(u) for u in uids), item)
        if typ != "OK":
            raise ImapError(f"{self.account}: FETCH failed")
        wanted = set(uids)
        return {u: d for u, d in parse_fetch(data).items() if u in wanted}

    def fetch_headers(self, uids: list[int]) -> list[HeaderRecord]:
        self._folder()
        out: list[HeaderRecord] = []
        for i in range(0, len(uids), FETCH_BATCH):
            for uid, d in sorted(self._fetch(uids[i : i + FETCH_BATCH], HEADER_ITEM).items()):
                hb = next((v for k, v in d.items() if k.startswith("BODY[HEADER")), b"")
                if isinstance(hb, str):
                    hb = hb.encode("utf-8", "surrogateescape")
                size = d.get("RFC822.SIZE")
                out.append(
                    HeaderRecord(uid, int(size) if isinstance(size, str) and size.isdigit() else 0, hb or b"")
                )
        return out

    def fetch_structures(
        self, uids: list[int], *, fresh: bool = False
    ) -> dict[int, Structure | StructureError]:
        f = self._folder()
        out: dict[int, Structure | StructureError] = {}
        todo = [u for u in uids if fresh or (f.name, f.uidvalidity, u) not in self._structures]
        for u in uids:
            if u not in todo:
                out[u] = self._structures[(f.name, f.uidvalidity, u)]
        for i in range(0, len(todo), FETCH_BATCH):
            batch = todo[i : i + FETCH_BATCH]
            try:
                parsed = self._fetch(batch, STRUCTURE_ITEM)
            except ResponseParseError:
                if len(batch) == 1:
                    out[batch[0]] = StructureError("unparseable server response")
                    continue
                parsed = {}  # one bad message poisoned the batch: retry individually
                for u in batch:
                    out.update(self.fetch_structures([u], fresh=fresh))
                continue
            for u in batch:
                try:
                    st = parse_bodystructure(parsed.get(u, {}).get("BODYSTRUCTURE"))
                except StructureError as e:
                    out[u] = e
                    continue
                self._structures[(f.name, f.uidvalidity, u)] = st
                out[u] = st
        return out

    def _structure(self, uid: int, *, fresh: bool = False) -> Structure:
        st = self.fetch_structures([uid], fresh=fresh).get(uid)
        if not isinstance(st, Structure):
            raise ImapError(f"{self.account}: message structure unavailable")
        return st

    def _fetch_part_bytes(self, uid: int, part_id: str, cap: int) -> bytes | None:
        """Fetch at most cap+1 bytes of one part. Returns None if the part exceeds cap."""
        item = f"(UID BODY.PEEK[{part_id}]<0.{cap + 1}>)"
        d = self._fetch([uid], item).get(uid, {})
        val = next((v for k, v in d.items() if k.startswith(f"BODY[{part_id}]")), _MISSING)
        if val is _MISSING:
            raise ImapError(f"{self.account}: server did not return the requested part")
        if val is None:
            return b""
        b = val if isinstance(val, bytes) else str(val).encode("utf-8", "surrogateescape")
        return None if len(b) > cap else b

    @staticmethod
    def _check_part_id(part_id: str) -> None:
        if not isinstance(part_id, str) or not PART_ID_RE.match(part_id):
            raise PolicyError("invalid part id")

    def fetch_text_part(self, uid: int, part_id: str) -> bytes | None:
        """One body text part (text/plain or text/html, not an attachment). None if over the size cap."""
        self._check_part_id(part_id)
        part = self._structure(uid).part(part_id)
        if part is None:
            raise PolicyError("no such part")
        if not part.is_body_text:
            raise PolicyError("part is an attachment; attachments require human approval")
        return self._fetch_part_bytes(uid, part_id, self._max_part_bytes)

    def fetch_message(self, uid: int) -> FetchedMessage:
        """Headers, structure, and body text parts of one message. Never attachments."""
        f = self._folder()
        headers = self.fetch_headers([uid])
        if not headers:
            raise ImapError(f"{self.account}: message not found")
        st = self.fetch_structures([uid]).get(uid)
        structure = st if isinstance(st, Structure) else None
        parts: dict[str, bytes | None] = {}
        if structure is not None:
            for p in structure.body_text_parts():
                parts[p.part_id] = self._fetch_part_bytes(uid, p.part_id, self._max_part_bytes)
        h = headers[0]
        return FetchedMessage(
            self.account, f.name, f.uidvalidity, uid, h.size, h.header_bytes, structure, parts
        )

    def fetch_approved_attachment(self, approval: Approval, *, now: float | None = None) -> bytes:
        if approval.account != self.account:
            raise PolicyError("approval is for a different account")
        if approval.expired(now):
            raise PolicyError("approval has expired; request it again")
        self._check_part_id(approval.part_id)
        info = self.examine(approval.folder)
        if info.uidvalidity != approval.uidvalidity:
            raise PolicyError("mailbox changed since approval; request it again")
        part = self._structure(approval.uid, fresh=True).part(approval.part_id)
        if part is None or part.is_body_text:
            raise PolicyError("approved item is not an attachment of this message")
        if part.size > self._max_attachment_bytes:
            raise PolicyError(f"attachment exceeds the size limit ({self._max_attachment_bytes} bytes)")
        data = self._fetch_part_bytes(approval.uid, approval.part_id, self._max_attachment_bytes)
        if data is None:
            raise PolicyError(f"attachment exceeds the size limit ({self._max_attachment_bytes} bytes)")
        return data
