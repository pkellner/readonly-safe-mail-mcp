"""IMAP response parsing, MIME structure (from BODYSTRUCTURE), and part decoding.

Pure functions, no I/O. All input is treated as hostile: it comes from the mail
server, which relays attacker-controlled mail.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import quopri
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.utils import decode_rfc2231
from urllib.parse import unquote_to_bytes

MAX_DEPTH = 20  # MIME nesting
MAX_PARTS = 200  # leaves per message
MAX_BODY_TEXT_PARTS = 20
MAX_RESPONSE_NESTING = 64

PART_ID_PATTERN = r"[1-9]\d{0,3}(?:\.[1-9]\d{0,3}){0,7}"
PART_ID_RE = re.compile(rf"^{PART_ID_PATTERN}$")


class ResponseParseError(ValueError):
    """An IMAP server response could not be parsed."""


class StructureError(ValueError):
    """A message's MIME structure is malformed or exceeds limits."""


class DecodeError(ValueError):
    """A body part could not be decoded with certainty."""


# --- IMAP response parsing --------------------------------------------------
#
# imaplib returns responses as a list mixing bytes lines and (head, literal)
# tuples. We flatten that into one buffer with literal placeholders, tokenize,
# and build nested lists: atoms/quoted strings -> str, NIL -> None,
# literals -> bytes, parenthesized lists -> list.

_OPEN, _CLOSE = object(), object()


class _Atom(str):
    pass


_LITERAL_SUFFIX_RE = re.compile(rb"\{\d+\}\s*$")
_ATOM_STOP = frozenset(b'() "\x00\t\r\n')


def _flatten(data) -> tuple[bytes, list[bytes]]:
    buf = bytearray()
    lits: list[bytes] = []
    for item in data or ():
        if item is None:
            continue
        if isinstance(item, tuple):
            head = item[0] if isinstance(item[0], bytes) else str(item[0]).encode()
            lit = item[1] if len(item) > 1 else b""
            if isinstance(lit, str):
                lit = lit.encode("utf-8", "surrogateescape")
            if b"\x00" in head:
                raise ResponseParseError("NUL byte in response")
            buf += _LITERAL_SUFFIX_RE.sub(b"", head) + b" \x00" + str(len(lits)).encode() + b"\x00 "
            lits.append(bytes(lit))
        else:
            line = item if isinstance(item, bytes) else str(item).encode()
            if b"\x00" in line:
                raise ResponseParseError("NUL byte in response")
            buf += line + b" "
    return bytes(buf), lits


def _tokenize(buf: bytes, lits: list[bytes]) -> Iterator:
    i, n = 0, len(buf)
    while i < n:
        c = buf[i]
        if c in b" \t\r\n":
            i += 1
        elif c == 0x28:
            yield _OPEN
            i += 1
        elif c == 0x29:
            yield _CLOSE
            i += 1
        elif c == 0x22:
            j, out = i + 1, bytearray()
            while True:
                if j >= n:
                    raise ResponseParseError("unterminated quoted string")
                ch = buf[j]
                if ch == 0x5C and j + 1 < n:
                    out.append(buf[j + 1])
                    j += 2
                    continue
                if ch == 0x22:
                    break
                out.append(ch)
                j += 1
            yield out.decode("utf-8", "replace")
            i = j + 1
        elif c == 0x00:
            j = buf.find(b"\x00", i + 1)
            if j < 0:
                raise ResponseParseError("bad literal marker")
            yield lits[int(buf[i + 1 : j])]
            i = j + 1
        else:
            # Atom. Brackets may contain spaces and parens: BODY[HEADER.FIELDS (FROM TO)]<0>
            j, depth = i, 0
            while j < n:
                ch = buf[j]
                if ch == 0x5B:
                    depth += 1
                elif ch == 0x5D and depth:
                    depth -= 1
                elif depth == 0 and ch in _ATOM_STOP:
                    break
                j += 1
            yield _Atom(buf[i:j].decode("ascii", "replace"))
            i = j


def parse_response(data) -> list:
    buf, lits = _flatten(data)
    stack: list[list] = [[]]
    for tok in _tokenize(buf, lits):
        if tok is _OPEN:
            if len(stack) > MAX_RESPONSE_NESTING:
                raise ResponseParseError("response nested too deeply")
            stack.append([])
        elif tok is _CLOSE:
            if len(stack) == 1:
                raise ResponseParseError("unbalanced ')'")
            done = stack.pop()
            stack[-1].append(done)
        elif isinstance(tok, _Atom):
            stack[-1].append(None if tok.upper() == "NIL" else str(tok))
        else:
            stack[-1].append(tok)
    if len(stack) != 1:
        raise ResponseParseError("unbalanced '('")
    return stack[0]


def parse_fetch(data) -> dict[int, dict[str, object]]:
    """FETCH responses -> {uid: {ITEM-NAME: value}}. Responses without a UID are ignored."""
    items = parse_response(data)
    out: dict[int, dict[str, object]] = {}
    i = 0
    while i + 1 < len(items):
        seq, lst = items[i], items[i + 1]
        if isinstance(seq, str) and seq.isdigit() and isinstance(lst, list):
            d = {k.upper(): v for k, v in zip(lst[0::2], lst[1::2], strict=False) if isinstance(k, str)}
            uid = d.get("UID")
            if isinstance(uid, str) and uid.isdigit():
                out.setdefault(int(uid), {}).update(d)
            i += 2
        else:
            i += 1
    return out


def parse_list(data) -> list[tuple[tuple[str, ...], str]]:
    """LIST responses -> [(flags, name)]."""
    items = parse_response(data)
    out = []
    i = 0
    while i + 2 < len(items):
        flags, _delim, name = items[i], items[i + 1], items[i + 2]
        if isinstance(flags, list) and isinstance(name, str | bytes):
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            out.append((tuple(str(f) for f in flags if isinstance(f, str)), name))
            i += 3
        else:
            i += 1
    return out


# --- MIME structure ---------------------------------------------------------


@dataclass(frozen=True)
class Part:
    part_id: str
    maintype: str
    subtype: str
    params: dict[str, str] = field(hash=False)
    encoding: str
    size: int
    disposition: str | None
    filename: str | None  # decoded but NOT sanitized

    @property
    def content_type(self) -> str:
        return f"{self.maintype}/{self.subtype}"

    @property
    def is_body_text(self) -> bool:
        return (
            self.maintype == "text"
            and self.subtype in ("plain", "html")
            and self.disposition in (None, "inline")
            and not self.filename
        )


@dataclass(frozen=True)
class Multipart:
    part_id: str  # "" for the root
    subtype: str
    children: tuple


@dataclass(frozen=True)
class Structure:
    root: Part | Multipart
    leaves: tuple[Part, ...]

    def part(self, part_id: str) -> Part | None:
        return next((p for p in self.leaves if p.part_id == part_id), None)

    def body_text_parts(self) -> list[Part]:
        return [p for p in self.leaves if p.is_body_text]

    def attachments(self) -> list[Part]:
        return [p for p in self.leaves if not p.is_body_text]

    def display_parts(self, prefer: str = "plain") -> list[Part]:
        return _display(self.root, prefer)


def _display(node: Part | Multipart, prefer: str) -> list[Part]:
    if isinstance(node, Part):
        return [node] if node.is_body_text else []
    if node.subtype == "alternative":
        options = [o for o in (_display(c, prefer) for c in node.children) if o]
        if not options:
            return []
        for o in options:
            if all(p.subtype == prefer for p in o):
                return o
        return options[-1]  # RFC 2046: last alternative is the richest
    out: list[Part] = []
    for c in node.children:
        out += _display(c, prefer)
    return out


@dataclass(frozen=True)
class FetchedMessage:
    """Everything sync retrieves for one message. Attachments are never in here."""

    account: str
    folder: str
    uidvalidity: int
    uid: int
    size: int
    header_bytes: bytes
    structure: Structure | None  # None if BODYSTRUCTURE was unparseable
    text_parts: dict[str, bytes | None]  # body text part id -> raw bytes; None = over size cap


def _s(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    if isinstance(v, str):
        return v
    raise StructureError("expected a string in BODYSTRUCTURE")


def _int(v) -> int:
    s = _s(v) if not isinstance(v, list) else None
    return int(s) if s and s.isdigit() else 0


def _params(v) -> dict[str, str]:
    if not isinstance(v, list):
        return {}
    out: dict[str, str] = {}
    for k, val in zip(v[0::2], v[1::2], strict=False):
        ks, vs = _s(k), _s(val)
        if ks is not None and vs is not None:
            out[ks.lower()] = vs
    return out


def _decode_2047(s: str) -> str:
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return s


def _decode_2231(s: str) -> str:
    charset, _lang, text = decode_rfc2231(s)  # (None, None, s) when there is no charset'lang' prefix
    try:
        return unquote_to_bytes(text).decode(charset or "utf-8", "replace")
    except LookupError:
        return unquote_to_bytes(text).decode("utf-8", "replace")


def _param_value(params: dict[str, str], key: str) -> str | None:
    if key in params:
        return _decode_2047(params[key])
    if key + "*" in params:
        return _decode_2231(params[key + "*"])
    cont = []
    for k, v in params.items():
        m = re.fullmatch(re.escape(key) + r"\*(\d+)(\*?)", k)
        if m:
            cont.append((int(m.group(1)), bool(m.group(2)), v))
    if not cont:
        return None
    cont.sort()
    joined = "".join(v for _, _, v in cont)
    return _decode_2231(joined) if cont[0][1] else _decode_2047(joined)


def parse_bodystructure(value) -> Structure:
    counter = [0]
    root = _node(value, "", 0, counter)
    leaves: list[Part] = []

    def walk(n):
        if isinstance(n, Part):
            leaves.append(n)
        else:
            for c in n.children:
                walk(c)

    walk(root)
    if sum(1 for p in leaves if p.is_body_text) > MAX_BODY_TEXT_PARTS:
        raise StructureError("too many body text parts")
    return Structure(root, tuple(leaves))


def _node(v, prefix: str, depth: int, counter: list[int]) -> Part | Multipart:
    if depth > MAX_DEPTH:
        raise StructureError("MIME nesting too deep")
    if not isinstance(v, list) or not v:
        raise StructureError("malformed BODYSTRUCTURE")
    if isinstance(v[0], list):
        children, i = [], 0
        while i < len(v) and isinstance(v[i], list):
            cid = f"{prefix}.{i + 1}" if prefix else str(i + 1)
            children.append(_node(v[i], cid, depth + 1, counter))
            i += 1
        subtype = (_s(v[i]) or "mixed").lower() if i < len(v) else "mixed"
        return Multipart(prefix, subtype, tuple(children))
    counter[0] += 1
    if counter[0] > MAX_PARTS:
        raise StructureError("too many MIME parts")
    return _leaf(v, prefix or "1")


def _leaf(v: list, part_id: str) -> Part:
    if len(v) < 7:
        raise StructureError("BODYSTRUCTURE part has too few fields")
    maintype = (_s(v[0]) or "").lower()
    subtype = (_s(v[1]) or "").lower()
    params = _params(v[2])
    encoding = (_s(v[5]) or "7bit").lower()
    size = _int(v[6])
    if maintype == "text":
        ext = 8
    elif maintype == "message" and subtype in ("rfc822", "global"):
        ext = 10
    else:
        ext = 7
    disp_v = v[ext + 1] if len(v) > ext + 1 else None
    disposition, dparams = None, {}
    if isinstance(disp_v, list) and disp_v:
        disposition = (_s(disp_v[0]) or "").lower() or None
        dparams = _params(disp_v[1] if len(disp_v) > 1 else None)
    filename = _param_value(dparams, "filename") or _param_value(params, "name")
    if maintype == "message" and not filename:
        filename = "attached-message.eml"  # forwarded messages are attachments, never descended into
    return Part(part_id, maintype, subtype, params, encoding, size, disposition, filename)


# --- decoding ---------------------------------------------------------------


def decode_part(raw: bytes, encoding: str, charset: str | None) -> str:
    """Transfer-decode and charset-decode one part. Raises DecodeError rather than guessing."""
    enc = (encoding or "7bit").strip().lower()
    if enc == "base64":
        compact = re.sub(rb"\s+", b"", raw)
        compact += b"=" * (-len(compact) % 4)
        try:
            data = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError) as e:
            raise DecodeError("invalid base64") from e
    elif enc == "quoted-printable":
        data = quopri.decodestring(raw)
    elif enc in ("7bit", "8bit", "binary"):
        data = raw
    else:
        raise DecodeError("unsupported transfer encoding")

    declared = (charset or "").strip().lower() or "us-ascii"
    for candidate in dict.fromkeys([declared, "utf-8"]):
        try:
            codecs.lookup(candidate)
            return data.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    raise DecodeError("text could not be decoded with its declared charset or UTF-8")
