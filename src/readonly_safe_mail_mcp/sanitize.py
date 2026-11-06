"""The sanitizer: the single path every piece of mail text takes before it is
indexed or returned (PLAN.md section 4.1).

Fixed order. Each stage only produces characters that later stages handle:
  1. decode       transfer encoding + charset                    (mime.decode_part)
  2. html -> text entities decoded; script/style dropped; all attributes
                  discarded; hidden and quoted regions MARKED, not yet removed
  3. normalize    NFKC; control/format chars removed               (textnorm)
  4. classify     sender domain + keywords over the FULL text of EVERY body
                  alternative, including hidden and quoted regions
  5. strip        hidden regions always; quoted replies unless that empties the body
  6. redact       URLs, bare domains, IPs, long tokens, codes
  7. truncate

Any decode or structure failure withholds the body. Raw content is never a fallback.
Withhold messages are generic and never echo what matched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email import policy
from email.parser import BytesHeaderParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

from .config import Rules
from .mime import DecodeError, FetchedMessage, Structure, decode_part
from .textnorm import (
    HIDDEN_CLOSE,
    HIDDEN_OPEN,
    QUOTE_CLOSE,
    QUOTE_OPEN,
    display_form,
    strip_sentinels,
)

CODE_MASK = "[code hidden]"
LINK_MASK = "[link removed]"
WITHHELD_SECURITY = "[Body withheld: classified as an account-security email. Read it in your mail client.]"
WITHHELD_UNDECODABLE = "[Body withheld: it could not be decoded safely.]"
WITHHELD_STRUCTURE = "[Body withheld: the message structure could not be parsed.]"
WITHHELD_TOO_LARGE = "[Body withheld: a text part exceeds the size limit.]"


# --- results ----------------------------------------------------------------


@dataclass(frozen=True)
class HeaderView:
    from_: str
    to: str
    cc: str
    subject: str
    date: str
    timestamp: float | None
    from_addr: str
    # Internal identifiers used only for threading. Never returned to the assistant.
    message_id: str
    in_reply_to: str
    references: str


@dataclass(frozen=True)
class AttachmentMeta:
    part_id: str
    filename: str
    content_type: str
    size: int


@dataclass(frozen=True)
class SanitizedMessage:
    headers: HeaderView
    body: str
    withheld: bool
    attachments: tuple[AttachmentMeta, ...]
    notes: tuple[str, ...]


# --- stage 2: HTML -> text --------------------------------------------------

_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
_DROP = frozenset({"script", "style", "template"})  # never used, not even for classification
_HIDE = frozenset({"head", "title"})  # classified, never displayed
_BLOCK = frozenset(
    "address article aside blockquote center dd div dl dt figcaption figure footer form "
    "h1 h2 h3 h4 h5 h6 header hr li main nav ol p pre section table tbody thead tfoot tr ul".split()
)
_CELL = frozenset({"td", "th"})
_QUOTE_CLASSES = frozenset({"gmail_quote", "yahoo_quoted", "moz-cite-prefix"})
_QUOTE_REST_IDS = frozenset({"divRplyFwdMsg", "appendonsend"})  # Outlook: everything after is the quote
_HIDDEN_STYLE_RE = re.compile(
    r"display:none|visibility:hidden|mso-hide:all"
    r"|font-size:0(?:\.0+)?(?:px|pt|em|rem|%)?(?:;|!|$)"
    r"|opacity:0(?:\.0+)?(?:;|!|$)"
)
_ZERO_HEIGHT_RE = re.compile(r"(?:max-)?height:0(?:px)?(?:;|!|$)")


def _style_hidden(style: str) -> bool:
    s = re.sub(r"\s+", "", style.lower())
    return bool(_HIDDEN_STYLE_RE.search(s)) or (bool(_ZERO_HEIGHT_RE.search(s)) and "overflow:hidden" in s)


class _HtmlText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)  # entities decoded here, before normalization
        self.out: list[str] = []
        self.stack: list[tuple[str, bool, bool, bool]] = []  # (tag, drop, hidden, quote)
        self.drop = 0
        self.pre = 0
        self.rest_quoted = False

    def _open(self, tag: str, attrs) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if not self.rest_quoted and a.get("id", "") in _QUOTE_REST_IDS:
            self.rest_quoted = True
            self.out.append(QUOTE_OPEN)
        if tag in _BLOCK or tag == "br":
            self.out.append("\n")
        elif tag in _CELL:
            self.out.append(" ")
        if tag in _VOID:
            return
        drop = tag in _DROP
        hidden = tag in _HIDE or "hidden" in a or _style_hidden(a.get("style", ""))
        quote = (tag == "blockquote" and a.get("type", "").lower() == "cite") or bool(
            set(a.get("class", "").split()) & _QUOTE_CLASSES
        )
        self.stack.append((tag, drop, hidden, quote))
        self.drop += drop
        self.pre += tag == "pre"
        if hidden:
            self.out.append(HIDDEN_OPEN)
        if quote:
            self.out.append(QUOTE_OPEN)

    def _close(self, tag: str, drop: bool, hidden: bool, quote: bool) -> None:
        self.drop -= drop
        self.pre -= tag == "pre"
        if quote:
            self.out.append(QUOTE_CLOSE)
        if hidden:
            self.out.append(HIDDEN_CLOSE)

    def handle_starttag(self, tag, attrs):
        self._open(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self._open(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if not any(t == tag for t, *_ in self.stack):
            return  # stray end tag
        while self.stack:
            entry = self.stack.pop()
            self._close(*entry)
            if entry[0] == tag:
                break
        if tag in _BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self.drop:
            return
        data = strip_sentinels(data)  # attacker-supplied markers never survive
        if not self.pre:
            data = re.sub(r"\s+", " ", data)
        self.out.append(data)

    def finish(self) -> str:
        self.close()
        while self.stack:
            self._close(*self.stack.pop())
        if self.rest_quoted:
            self.out.append(QUOTE_CLOSE)
        return "".join(self.out)


def html_to_text(html: str) -> str:
    """Visible text with hidden/quoted regions marked. Attributes (href, src, alt...) never appear."""
    parser = _HtmlText()
    try:
        parser.feed(html)
        return parser.finish()
    except Exception as e:  # HTMLParser can raise on pathological input
        raise DecodeError("HTML could not be parsed") from e


# --- stage 5: quotes and hidden regions -------------------------------------

_ON_WROTE_RE = re.compile(r"^on\b.{0,300}\bwrote:\s*$", re.I | re.S)
_ORIGINAL_RE = re.compile(r"^-{2,}\s*original message\s*-{2,}$", re.I)
_HEADER_LINE_RE = re.compile(r"^\*?(from|sent|date|to|subject|cc):\*?\s", re.I)
_FORWARDED_RE = re.compile(r"forwarded message|begin forwarded message", re.I)


def _attribution_span(lines: list[str], i: int) -> int:
    s = lines[i].strip()
    if not s.lower().startswith("on "):
        return 0
    if _ON_WROTE_RE.match(s):
        return 1
    if i + 1 < len(lines) and _ON_WROTE_RE.match(s + " " + lines[i + 1].strip()):
        return 2
    return 0


def _is_reply_header_block(lines: list[str], i: int) -> bool:
    m = _HEADER_LINE_RE.match(lines[i].strip())
    if not m or m.group(1).lower() != "from":
        return False
    following = sum(1 for ln in lines[i + 1 : i + 6] if _HEADER_LINE_RE.match(ln.strip()))
    if following < 2:
        return False
    before = [ln for ln in lines[max(0, i - 3) : i] if ln.strip()]
    return not any(_FORWARDED_RE.search(ln) for ln in before)  # forwarded content is kept


def mark_plain_quotes(text: str) -> str:
    lines = text.split("\n")
    prefixed = [ln.lstrip().startswith(">") for ln in lines]
    quoted = list(prefixed)
    for i in range(len(lines)):
        if quoted[i]:
            continue
        span = _attribution_span(lines, i)
        if span:
            for k in range(i, i + span):
                quoted[k] = True
            if not any(prefixed[i + span :]):  # unprefixed quote follows: the rest is quoted
                for k in range(i + span, len(lines)):
                    quoted[k] = True
                break
            continue  # inline replies: only the ">" lines are quotes
        if _ORIGINAL_RE.match(lines[i].strip()) or _is_reply_header_block(lines, i):
            for k in range(i, len(lines)):
                quoted[k] = True
            break
    # A quoted line's newline goes inside the region, so removing it leaves no blank line.
    last = len(lines) - 1
    out = []
    for i, (ln, q) in enumerate(zip(lines, quoted, strict=True)):
        sep = "\n" if i < last else ""
        out.append(f"{QUOTE_OPEN}{ln}{sep}{QUOTE_CLOSE}" if q else ln + sep)
    return "".join(out)


def strip_regions(text: str, open_ch: str, close_ch: str) -> str:
    out, depth = [], 0
    for ch in text:
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --- stage 6: redaction -----------------------------------------------------

_COMMON_TLDS = frozenset(
    "com net org io co us uk ca de fr au in jp me info biz edu gov mil app dev ai ly gl to "
    "link page site online xyz tech cloud shop store club live email".split()
)
URL_RE = re.compile(r"""(?:[a-z][a-z0-9+.\-]{1,20}://|www\.)[^\s<>"'()\[\]{}]+""", re.I)
IP_RE = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?(?:[/?#][^\s<>\"']*)?(?!\w|\.\d)")
BARE_DOMAIN_RE = re.compile(
    r"(?<![\w@.\-])(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+([a-z]{2,24})(?::\d{1,5})?(?![\w@\-])"
    r"([/?#][^\s<>\"']*)?",
    re.I,
)
LONG_TOKEN_RE = re.compile(r"(?<![\w\-+/=])[A-Za-z0-9_\-+/=]{16,}(?![\w\-+/=])")
ALNUM_RE = re.compile(r"(?<!\w)[A-Za-z0-9]{6,40}(?!\w)")
DIGIT_CODE_RE = re.compile(r"(?<![\w$€£¥.,])(?:\d[ -]?){3,9}\d(?![\w%]|[.,]\d)")
_AMOUNT_AFTER_RE = re.compile(r"\s?(?:dollars?|usd|eur|euros?|pounds?|gbp|k\b|%|percent)", re.I)
_ISO_DATE_RE = re.compile(r"(?:19|20)\d\d-\d\d-\d\d")


def _transitions(s: str) -> int:
    return sum(1 for a, b in zip(s, s[1:], strict=False) if a.isdigit() != b.isdigit())


def redact(text: str) -> tuple[str, int, int]:
    """Returns (text, links_removed, codes_hidden)."""
    links = codes = 0

    def link(_m):
        nonlocal links
        links += 1
        return LINK_MASK

    def domain(m):
        if m.group(2) or m.group(1).lower() in _COMMON_TLDS:
            return link(m)
        return m.group(0)

    def code(_m):
        nonlocal codes
        codes += 1
        return CODE_MASK

    def long_token(m):
        s = m.group(0)
        digits, letters = sum(c.isdigit() for c in s), sum(c.isalpha() for c in s)
        return code(m) if digits >= 3 and letters >= 3 else s

    def alnum(m):
        s = m.group(0)
        if not (any(c.isdigit() for c in s) and any(c.isalpha() for c in s)):
            return s
        if (not any(c.islower() for c in s) and len(s) <= 12) or _transitions(s) >= 3:
            return code(m)
        return s

    def digits(m):
        s, start, end = m.group(0), m.start(), m.end()
        if re.search(r"[$€£¥]\s?$", m.string[max(0, start - 2) : start]):
            return s  # amount: $2500
        if _AMOUNT_AFTER_RE.match(m.string, end):
            return s  # amount: 2500 dollars, 40%
        if s.isdigit() and len(s) == 4 and 1900 <= int(s) <= 2099:
            return s  # year
        if _ISO_DATE_RE.fullmatch(s):
            return s
        return code(m)

    text = URL_RE.sub(link, text)
    text = IP_RE.sub(link, text)
    text = BARE_DOMAIN_RE.sub(domain, text)
    text = LONG_TOKEN_RE.sub(long_token, text)
    text = ALNUM_RE.sub(alnum, text)
    text = DIGIT_CODE_RE.sub(digits, text)
    return text, links, codes


def sanitize_text(text: str) -> str:
    """Single-line fields: subjects, display names, filenames."""
    t, _, _ = redact(display_form(strip_sentinels(text or "")))
    return " ".join(t.split())


# --- headers ----------------------------------------------------------------


def _header(msg, name: str) -> str:
    try:
        return str(msg.get(name, "") or "")
    except Exception:
        return ""


def _addresses(value: str) -> tuple[str, str]:
    try:
        pairs = [(n, a) for n, a in getaddresses([value]) if n or a]
    except Exception:
        pairs = []
    shown = [f"{' '.join(n.split())} <{a}>" if n.strip() else a for n, a in pairs[:50]]
    text = ", ".join(shown) + (f", (+{len(pairs) - 50} more)" if len(pairs) > 50 else "")
    return sanitize_text(text), (pairs[0][1].lower() if pairs else "")


def parse_headers(header_bytes: bytes) -> tuple[HeaderView, str, str]:
    """Returns (sanitized view, raw subject, raw From) -- raw values are for classification only."""
    msg = BytesHeaderParser(policy=policy.default).parsebytes(header_bytes or b"")
    raw_subject, raw_from, date = _header(msg, "Subject"), _header(msg, "From"), _header(msg, "Date")
    from_, from_addr = _addresses(raw_from)
    try:
        ts = parsedate_to_datetime(date).timestamp() if date else None
    except Exception:
        ts = None
    view = HeaderView(
        from_=from_,
        to=_addresses(_header(msg, "To"))[0],
        cc=_addresses(_header(msg, "Cc"))[0],
        subject=sanitize_text(raw_subject),
        date=" ".join(date.split()),
        timestamp=ts,
        from_addr=from_addr,
        message_id=_header(msg, "Message-ID").strip(),
        in_reply_to=_header(msg, "In-Reply-To").strip(),
        references=" ".join(_header(msg, "References").split()),
    )
    return view, raw_subject, raw_from


# --- the pipeline -----------------------------------------------------------


def _display_body(structure: Structure, texts: dict[str, str]) -> tuple[str, bool]:
    """Pick the display alternative and strip hidden + quoted regions. Returns (body, quotes_removed)."""
    for prefer in ("plain", "html"):
        chunks, removed = [], False
        for p in structure.display_parts(prefer):
            t = texts[p.part_id]
            if p.subtype == "plain":
                t = mark_plain_quotes(t)
            t = strip_regions(t, HIDDEN_OPEN, HIDDEN_CLOSE)
            unquoted = strip_sentinels(strip_regions(t, QUOTE_OPEN, QUOTE_CLOSE))
            if unquoted.strip():
                removed = removed or len(unquoted) < len(strip_sentinels(t))
            else:
                unquoted = strip_sentinels(t)  # the body is only a quote: keep it rather than show nothing
            chunks.append(unquoted)
        body = _tidy("\n\n".join(chunks))
        if body:
            return body, removed
    return "", False


def sanitize_message(fm: FetchedMessage, rules: Rules) -> SanitizedMessage:
    headers, raw_subject, raw_from = parse_headers(fm.header_bytes)

    def result(body: str, withheld: bool, attachments=(), notes=()) -> SanitizedMessage:
        return SanitizedMessage(headers, body, withheld, tuple(attachments), tuple(notes))

    if fm.structure is None:
        return result(WITHHELD_STRUCTURE, True, notes=["body withheld"])
    attachments = [
        AttachmentMeta(p.part_id, sanitize_text(p.filename or "(unnamed)"), p.content_type, p.size)
        for p in fm.structure.attachments()
    ]

    # Stages 1-3 for every body text part (every alternative).
    texts: dict[str, str] = {}
    for part in fm.structure.body_text_parts():
        raw = fm.text_parts.get(part.part_id)
        if raw is None:
            return result(WITHHELD_TOO_LARGE, True, attachments, ["body withheld"])
        try:
            decoded = decode_part(raw, part.encoding, part.params.get("charset"))
            decoded = html_to_text(decoded) if part.subtype == "html" else strip_sentinels(decoded)
        except DecodeError:
            return result(WITHHELD_UNDECODABLE, True, attachments, ["body withheld"])
        texts[part.part_id] = display_form(decoded)

    # Stage 4: classify on everything, before anything is discarded.
    if rules.sender_is_blocked(raw_from) or rules.keyword_hit(raw_subject, *texts.values()):
        return result(WITHHELD_SECURITY, True, attachments, ["body withheld"])

    # Stages 5-7.
    body, quotes_removed = _display_body(fm.structure, texts)
    body, n_links, n_codes = redact(body)
    notes = []
    if quotes_removed:
        notes.append("quoted replies removed")
    if n_links:
        notes.append(f"{n_links} link(s) removed")
    if n_codes:
        notes.append(f"{n_codes} code(s) hidden")
    if len(body) > rules.max_body_chars:
        body = body[: rules.max_body_chars] + "\n[truncated]"
        notes.append("body truncated")
    return result(body, False, attachments, notes)
