"""Opening attachments: resolve -> human approval -> fetch one part -> sandboxed
extraction -> sanitizer (PLAN.md section 7). The MCP tool in server.py drives this."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from . import credentials
from .config import Config
from .extract import kind_for
from .imap import Approval, ImapError, MailSource
from .isolate import Runner, WorkerError, run_worker
from .sanitize import redact, sanitize_text
from .textnorm import display_form, strip_sentinels

MAX_ITEMS = 10
APPROVAL_TTL = 120.0
WITHHELD_ATTACHMENT = (
    "[Attachment withheld: it looks like account-security content. Open it in your mail client.]"
)
KIND_LABEL = {
    "pdf": "PDF",
    "docx": "Word",
    "xlsx": "Excel",
    "pptx": "PowerPoint",
    "text": "text",
    "html": "HTML",
    "vcf": "contact card",
    "ics": "calendar invite",
    "eml": "attached email",
    "image": "image",
}


@dataclass
class Target:
    mid: int
    part: str
    account: str
    folder: str
    uidvalidity: int
    uid: int
    name: str
    ctype: str
    size: int
    kind: str
    date: str
    sender: str
    subject: str

    @property
    def label(self) -> str:
        return f"{self.date} · {self.sender} · {self.subject} · {self.name} ({_size(self.size)})"

    @property
    def title(self) -> str:
        return f"#{self.mid} part {self.part}: {self.name} ({KIND_LABEL[self.kind]})"


@dataclass
class Opened:
    target: Target
    text: str = ""
    images: list[tuple[str, str]] = field(default_factory=list)  # (base64, format)
    notes: list[str] = field(default_factory=list)
    error: str | None = None


def _size(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB" if n >= 1_000_000 else f"{max(1, round(n / 1000))} KB"


def resolve(
    db: sqlite3.Connection, refs: list[tuple[int, str]], max_bytes: int
) -> tuple[list[Target], list[str]]:
    """Look up each (message id, part) in the index. Returns (openable targets, refusal lines)."""
    targets, refused = [], []
    seen = set()
    for mid, part in refs[:MAX_ITEMS]:
        part = str(part).strip()
        if (mid, part) in seen:
            continue
        seen.add((mid, part))
        r = db.execute("SELECT * FROM messages WHERE id=?", (int(mid),)).fetchone()
        if r is None:
            refused.append(f"#{mid} part {part}: no such message (ids come from search results)")
            continue
        if r["withheld"]:
            refused.append(f"#{mid}: attachments of withheld messages are not opened")
            continue
        try:
            atts = json.loads(r["attachments"] or "[]")
        except ValueError:
            atts = []
        a = next((x for x in atts if str(x.get("part")) == part), None)
        if a is None:
            listed = ", ".join(f"{x['part']}={x['name']}" for x in atts) or "none"
            refused.append(f"#{mid} part {part}: not an attachment of this message (parts: {listed})")
            continue
        kind = kind_for(a["type"], a["name"])
        if kind is None:
            refused.append(
                f"#{mid} part {part}: {a['name']} ({a['type']}) is not a supported type "
                "(supported: PDF, Word, Excel, PowerPoint, text/CSV, HTML, contact cards, calendar "
                "invites, attached emails, images)"
            )
            continue
        if a["size"] > max_bytes:
            refused.append(f"#{mid} part {part}: {a['name']} is larger than the {_size(max_bytes)} limit")
            continue
        targets.append(
            Target(
                int(mid),
                part,
                r["account"],
                r["folder"],
                r["uidvalidity"],
                r["uid"],
                a["name"],
                a["type"],
                a["size"],
                kind,
                (r["date"] or "")[:16],
                r["from_"] or "",
                r["subject"] or "",
            )
        )
    if len(refs) > MAX_ITEMS:
        refused.append(f"only the first {MAX_ITEMS} attachments are handled per request")
    return targets, refused


def sanitize_extracted(text: str, rules) -> tuple[str, list[str]]:
    clean = display_form(strip_sentinels(text))
    if rules.keyword_hit(clean):
        return WITHHELD_ATTACHMENT, ["withheld"]
    out, links, codes = redact(clean)
    notes = []
    if links:
        notes.append(f"{links} link(s) removed")
    if codes:
        notes.append(f"{codes} code(s) hidden")
    return out.strip(), notes


def open_approved(
    cfg: Config,
    targets: list[Target],
    runner: Runner,
    *,
    open_source: Callable[..., MailSource] | None = None,
    password_fn: Callable[[str], str] = credentials.get_password,
) -> list[Opened]:
    """Fetch and extract targets the human approved. Each approval is created here, bound
    to the exact item, used once, and expires quickly."""
    results: list[Opened] = []
    by_account: dict[str, list[Target]] = {}
    for t in targets:
        by_account.setdefault(t.account, []).append(t)
    for account, items in by_account.items():
        acct = cfg.accounts.get(account)
        if acct is None:
            results += [Opened(t, error="account is no longer configured") for t in items]
            continue
        try:
            src = (open_source or MailSource.open)(acct, password_fn(account))
        except Exception as e:
            msg = str(e) if isinstance(e, (ImapError, credentials.CredentialError)) else type(e).__name__
            results += [Opened(t, error=f"could not connect: {msg}") for t in items]
            continue
        with src:
            for t in items:
                results.append(_open_one(src, t, runner, acct.rules))
    return results


def _open_one(src: MailSource, t: Target, runner: Runner, rules) -> Opened:
    approval = Approval(
        t.account, t.folder, t.uidvalidity, t.uid, t.part, uuid.uuid4().hex, time.time() + APPROVAL_TTL
    )
    try:
        raw, part = src.fetch_approved_part(approval)
    except ImapError as e:
        return Opened(t, error=str(e))
    # The server must still describe the same item the human approved.
    if sanitize_text(part.filename or "(unnamed)") != t.name or part.content_type != t.ctype:
        return Opened(t, error="the attachment changed on the server since it was indexed; not opened")
    try:
        items = run_worker(runner, t.kind, raw, part.encoding, part.params.get("charset"))
    except WorkerError as e:
        return Opened(t, error=str(e))
    res = Opened(t)
    texts = [it["text"] for it in items if isinstance(it.get("text"), str)]
    if texts:
        res.text, res.notes = sanitize_extracted("\n\n".join(texts), rules)
    if "withheld" not in res.notes:
        res.images = [
            (it["image"], it.get("format", "png"))
            for it in items
            if isinstance(it.get("image"), str) and it.get("format") in ("png", "jpeg")
        ]
    if runner.name == "unsandboxed":
        res.notes.append("opened WITHOUT a sandbox (attachments: ask-unsandboxed)")
    return res
