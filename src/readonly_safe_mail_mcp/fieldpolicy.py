"""Field policy for contact cards and calendar events (issue #1, "Field policy").

Decides what of a contact card, or of an event's text, an AI may see. Calendar content is
writable by strangers (anyone can send an invitation), so every value is hostile input.
Takes vcal.py's parsed values; returns sanitized fields only.

Every field takes the same order:
  0. bound      a value over MAX_FIELD_INPUT_CHARS is not read at all
  1. decode     vCard 2.1 QUOTED-PRINTABLE / BASE64 with an allowed CHARSET, RFC 6868
                parameter carets, text unescaping, HTML entities; notes and descriptions
                with markup are reduced to their visible text as an HTML attachment is
  2. normalize  strip_sentinels, display_form, then two kinds of invisible characters the
                mail normalizer keeps: default-ignorable code points and marks on a digit
  3. classify   sensitive keywords over the item's text fields; an organizer in a blocked
                sender domain
  4. redact     single-line fields through sanitize_text, free text through
                sanitize_extracted, then digit runs too long for the mail redactor
  5. cap

Phones, emails, postal addresses and birthdays are kept only in validated shapes; a value
that does not match is dropped. A decode failure withholds that field. Raw text is never a
fallback. Only the properties named here are read; any other is ignored.

The event's timing (DTSTART, DTEND, DURATION, RRULE, RDATE, EXDATE, RECURRENCE-ID and their
TZIDs) is not read here: the calendar code validates it and stores it beside EventFields.
Alarms never reach this module: parse_icalendar skips components nested in an event.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import html
import quopri
import re
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from email.utils import getaddresses

from .attachments import sanitize_extracted
from .config import Rules
from .extract import _html_text
from .mime import DecodeError
from .sanitize import CODE_MASK, LONG_TOKEN_RE, redact, sanitize_text
from .textnorm import display_form, strip_sentinels
from .vcal import Prop, VCard, VEvent, _ical_unescape

# Bump whenever output for the same input could change. The mail SANITIZER_VERSION is separate.
FIELD_POLICY_VERSION = "2"

MAX_LINE_CHARS = 200
MAX_LABEL_CHARS = 40
MAX_ADDRESS_CHARS = 400
MAX_TEXT_CHARS = 5000
MAX_ITEMS = 20  # per kind (phones, emails, addresses) on one card
MAX_ATTENDEES = 30
# A value longer than this, before decoding, is not normalized or classified at all: free
# text is withheld, a single-line field is not shown. Far above every display cap.
MAX_FIELD_INPUT_CHARS = 100_000

WITHHELD_SECURITY = "[{} withheld: classified as account-security content.]"
WITHHELD_UNDECODABLE = "[{} withheld: it could not be decoded safely.]"
WITHHELD_TOO_LONG = "[{} withheld: too long to check.]"


# --- results ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    label: str  # a word from a fixed list, a sanitized custom label, or ""
    value: str


@dataclass(frozen=True)
class ContactFields:
    name: str
    org: str
    title: str
    nickname: str
    emails: tuple[Item, ...]
    phones: tuple[Item, ...]
    addresses: tuple[Item, ...]
    birthday: str  # "1990-01-02", "--01-02" without a year, or ""
    note: str
    note_withheld: bool
    dropped: tuple[str, ...]  # kinds of field present and never shown: "photo", "website", ...
    notes: tuple[str, ...]


@dataclass(frozen=True)
class Person:
    name: str
    address: str
    status: str  # attendee participation: accepted, declined, tentative, needs-action, delegated, ""


@dataclass(frozen=True)
class EventFields:
    title: str
    location: str
    description: str
    description_withheld: bool
    organizer: Person | None
    attendees: tuple[Person, ...]
    attendees_omitted: int
    status: str  # confirmed, tentative, cancelled, ""
    cancelled: bool
    invitation: str  # "not accepted", "acceptance unknown" (the user is not among the attendees), ""
    dropped: tuple[str, ...]
    notes: tuple[str, ...]


# --- decode and normalize ----------------------------------------------------------------


class _Undecodable(ValueError):
    pass


# The charsets vCard 2.1 senders use in practice, by Python codec name. Any other codec name
# (utf-7, unicode_escape, "undefined", ...) withholds the field: some rewrite the text.
_CHARSETS = frozenset(
    {
        "utf-8",
        "utf-16",
        "utf-16-le",
        "utf-16-be",
        "ascii",
        "iso8859-1",
        "iso8859-2",
        "iso8859-15",
        "cp1250",
        "cp1251",
        "cp1252",
        "koi8-r",
        "shift_jis",
        "euc_jp",
        "iso2022_jp",
        "gb2312",
        "gbk",
        "gb18030",
        "big5",
        "euc_kr",
    }
)


def _codec(charset: str) -> str:
    try:
        name = codecs.lookup(charset).name
    except LookupError as e:
        raise _Undecodable from e
    if name not in _CHARSETS:
        raise _Undecodable
    return name


def _decode(prop: Prop, raw: str) -> str:
    params = prop.params
    enc = params.get("ENCODING", "").upper()
    if "QUOTED-PRINTABLE" in params or "BASE64" in params:  # vCard 2.1 bare parameters
        enc = "QUOTED-PRINTABLE" if "QUOTED-PRINTABLE" in params else "BASE64"
    if enc in ("", "8BIT"):
        return raw
    if not raw.isascii():
        raise _Undecodable
    try:
        if enc == "QUOTED-PRINTABLE":
            data = quopri.decodestring(raw.encode("ascii"))
        elif enc in ("B", "BASE64"):
            data = base64.b64decode(re.sub(r"\s+", "", raw), validate=True)
        else:
            raise _Undecodable
        return data.decode(_codec(params.get("CHARSET") or "utf-8"))
    except (binascii.Error, UnicodeError) as e:
        raise _Undecodable from e


_CARET = {"n": "\n", "'": '"', "^": "^"}


def _param(prop: Prop, name: str) -> str:
    """RFC 6868: ^n, ^' and ^^ in a parameter value."""
    return re.sub(r"\^([n'^])", lambda m: _CARET[m.group(1)], prop.params.get(name, ""))


# Default_Ignorable_Code_Point (Unicode DerivedCoreProperties). display_form removes the
# format characters among them; the rest (U+034F, variation selectors, Hangul fillers, ...)
# render as nothing and would split a digit run past the redactor.
_IGNORABLE_RE = re.compile(
    r"[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e"
    r"\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8\U0001bca0-\U0001bca3"
    r"\U0001d173-\U0001d17a\U000e0000-\U000e0fff]"
)


def _drop_digit_marks(text: str) -> str:
    """Combining marks on a digit (U+0301 after a "4", keycaps) would also split a digit run."""
    if text.isascii():
        return text
    out: list[str] = []
    after_digit = False
    for c in text:
        if after_digit and unicodedata.category(c)[0] == "M":
            continue
        out.append(c)
        after_digit = c.isdigit()
    return "".join(out)


def _normalize(text: str) -> str:
    return _drop_digit_marks(_IGNORABLE_RE.sub("", display_form(strip_sentinels(text))))


_TAG_RE = re.compile(r"<[A-Za-z/!?]")


def _html(text: str, *, tags: bool = False) -> str:
    """Entities decoded, as a reader would see them; with `tags`, text with markup goes through
    the mail HTML-to-text step (visible text only, as for an HTML attachment)."""
    if tags and _TAG_RE.search(text):
        try:
            return _html_text(text)
        except DecodeError as e:
            raise _Undecodable from e
    return html.unescape(text)


def _ascii_like(decoded: str) -> bool:
    """Only ASCII, plus characters normalization removes or turns into a space."""
    return all(c.isascii() or unicodedata.category(c) in ("Cf", "Zs") for c in decoded)


# --- redact and cap -----------------------------------------------------------------------

# The mail redactor hides runs of 4 to 10 digits; a longer unbroken run passes it.
_LONG_DIGITS_RE = re.compile(r"\d{11,}")


def _cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _line(normalized: str, cap: int = MAX_LINE_CHARS) -> str:
    # Whitespace is collapsed before redaction: a tab or newline between digit groups would
    # otherwise pass the redactor and then be joined into "482 913" by sanitize_text.
    return _cap(_LONG_DIGITS_RE.sub(CODE_MASK, sanitize_text(" ".join(normalized.split()))), cap)


def display_line(text: str) -> str:
    """A server-supplied label, such as an address book's name, as one sanitized, capped line."""
    return _line(_normalize(_html(text))) if len(text) <= MAX_FIELD_INPUT_CHARS else ""


def _free_text(what: str, normalized: str, rules: Rules, withhold: bool) -> tuple[str, bool, list[str]]:
    if withhold:
        return WITHHELD_SECURITY.format(what), True, [f"{what.lower()} withheld"]
    # Runs of spaces and tabs become one space: the redactor joins digit groups across one space only.
    out, notes = sanitize_extracted(re.sub(r"[ \t]+", " ", normalized), rules)
    if "withheld" in notes:
        return WITHHELD_SECURITY.format(what), True, [f"{what.lower()} withheld"]
    out, n = _LONG_DIGITS_RE.subn(CODE_MASK, out)
    if n:
        notes.append(f"{n} long number(s) hidden")
    cap = min(MAX_TEXT_CHARS, rules.max_body_chars)
    if len(out) > cap:
        out = out[:cap] + "\n[truncated]"
        notes.append(f"{what.lower()} truncated")
    return out, False, notes


class _Reader:
    """Decodes and normalizes property values, counting the ones that fail or are too long."""

    def __init__(self) -> None:
        self.failed = 0
        self.too_long = 0

    def fits(self, prop: Prop) -> bool:
        if len(prop.value) + sum(len(v) for v in prop.params.values()) <= MAX_FIELD_INPUT_CHARS:
            return True
        self.too_long += 1
        return False

    def text(self, prop: Prop, *, tags: bool = False) -> str | None:
        if not self.fits(prop):
            return None
        try:
            return _normalize(_html(_ical_unescape(_decode(prop, prop.value)), tags=tags))
        except _Undecodable:
            self.failed += 1
            return None

    def parts(self, prop: Prop) -> tuple[str, ...] | None:
        if not self.fits(prop):
            return None
        try:
            return tuple(_normalize(_html(_decode(prop, p))) for p in prop.parts)
        except _Undecodable:
            self.failed += 1
            return None

    def first(self, props: list[Prop]) -> str:
        for p in props:
            t = self.text(p)
            if t is not None:
                return t
        return ""

    def free_text(
        self, what: str, props: list[Prop], rules: Rules, withhold: bool
    ) -> tuple[str, bool, list[str]]:
        """Every value of a free-text field (notes, descriptions) as one text, sanitized; withheld
        whole if any part cannot be decoded or all of them together are too long: a part of a
        field is never shown alone. ("", False, []) when there is none."""
        if sum(len(p.value) for p in props) > MAX_FIELD_INPUT_CHARS:
            return WITHHELD_TOO_LONG.format(what), True, [f"{what.lower()} withheld"]
        texts = []
        for p in props:
            try:
                texts.append(_normalize(_html(_ical_unescape(_decode(p, p.value)), tags=True)))
            except _Undecodable:
                self.failed += 1
                return WITHHELD_UNDECODABLE.format(what), True, [f"{what.lower()} withheld"]
        text = "\n\n".join(t for t in texts if t.strip())
        return _free_text(what, text, rules, withhold) if text.strip() else ("", False, [])

    def notes(self) -> list[str]:
        out = [f"{self.failed} field(s) could not be decoded"] if self.failed else []
        return out + ([f"{self.too_long} field(s) too long to show"] if self.too_long else [])


# --- validated shapes ---------------------------------------------------------------------

_PHONE_RE = re.compile(
    r"(?P<main>\+?(?:\(\d{1,5}\) ?|\d)(?:[ .\-]?\d|[ .\-]?\(\d{1,5}\) ?)*)"
    r"(?: ?(?:;ext=|ext\.? ?|x ?|# ?)(?P<ext>\d{1,6}))?",
    re.I | re.A,
)
_MAX_PHONE_CHARS = 40


def _phone(decoded: str) -> str | None:
    if not _ascii_like(decoded):
        return None  # fullwidth, Arabic-Indic and other look-alike digits
    s = " ".join(_normalize(decoded).split())
    if s[:4].lower() == "tel:":
        s = s[4:]
    if len(s) > _MAX_PHONE_CHARS or not _PHONE_RE.fullmatch(s):
        return None
    return s if 7 <= sum(c.isdigit() for c in s) <= 15 else None


def phone_digits(number: str) -> str:
    """Digits of a kept phone number without its extension, for search: '+1 (555) 0100' -> '15550100'."""
    m = _PHONE_RE.fullmatch(number)
    if m is None:
        raise ValueError("not a phone number kept by the field policy")
    return "".join(c for c in m.group("main") if c.isdigit())


_ADDR_SPEC_RE = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24}"
)


def _email(normalized: str) -> str | None:
    """One bare address, or None. A display name, a second address or a hidden code drops it."""
    s = normalized.strip()
    if len(s) > 254 or not _ADDR_SPEC_RE.fullmatch(s):
        return None
    if getaddresses([s]) != [("", s)]:
        return None
    return s if sanitize_text(s) == s else None


_POSTAL_RE = re.compile(r"[A-Za-z0-9]{2,8}(?:[ -][A-Za-z0-9]{1,4})?", re.A)
_DIGIT_RUN_RE = re.compile(r"#?\d+(?:[-/]\d+)?[.,;:]?")


def _postal_code(decoded: str) -> str | None:
    if not _ascii_like(decoded):
        return None
    s = " ".join(_normalize(decoded).split())
    return s if len(s) <= 10 and _POSTAL_RE.fullmatch(s) else None


def _address_text(normalized: str) -> str:
    """Links and long tokens removed; digit runs (house numbers, PO boxes) kept."""
    out = []
    for tok in normalized.split():
        if len(tok) < 16 and _DIGIT_RUN_RE.fullmatch(tok):
            out.append(tok)
            continue
        t = LONG_TOKEN_RE.sub(CODE_MASK, redact(tok)[0])
        out.append(_LONG_DIGITS_RE.sub(CODE_MASK, t))
    return " ".join(out)


def _address(parts: tuple[str, ...], raw_code: str | None) -> tuple[str, bool]:
    """(rendered address, postal code dropped). parts are normalized ADR components."""
    pobox, ext, street, locality, region, _, country = parts
    code = _postal_code(raw_code) if raw_code and raw_code.strip() else ""
    text = [_address_text(p.replace("\n", ", ")) for p in (pobox, ext, street, locality)]
    text.append(" ".join(x for x in (_address_text(region), code or "") if x))
    text.append(_address_text(country))
    return _cap(", ".join(t for t in text if t.strip(" ,")), MAX_ADDRESS_CHARS), code is None


_BDAY_RE = re.compile(
    r"(?:([0-9]{4})-?|--)([0-9]{2})-?([0-9]{2})(?:T[0-9]{2}(?::?[0-9]{2}){0,2}(?:Z|[+-][0-9]{2}(?::?[0-9]{2})?)?)?"
)


_MIN_BIRTH_YEAR = 1900


def _birthday(normalized: str, omit_year: str) -> str | None:
    """A year outside 1900 to next year drops the birthday: eight digits in BDAY are otherwise
    a way to show a code as a date."""
    m = _BDAY_RE.fullmatch(normalized.strip())
    if m is None:
        return None
    year, month, day = m.groups()
    if year is not None and year == omit_year:  # Apple: X-APPLE-OMIT-YEAR=1604
        year = None
    if year is not None and not _MIN_BIRTH_YEAR <= int(year) <= date.today().year + 1:
        return None
    try:
        date(int(year) if year else 2000, int(month), int(day))
    except ValueError:
        return None
    return f"{year}-{month}-{day}" if year else f"--{month}-{day}"


# --- labels -------------------------------------------------------------------------------

_LABEL_WORDS = {
    "home": "home",
    "work": "work",
    "cell": "mobile",
    "mobile": "mobile",
    "iphone": "iphone",
    "main": "main",
    "fax": "fax",
    "homefax": "home fax",
    "workfax": "work fax",
    "pager": "pager",
    "school": "school",
    "other": "other",
}
_APPLE_LABEL_RE = re.compile(r"_\$!<([A-Za-z]{1,20})>!\$_")


def _group_labels(card: VCard, reader: _Reader) -> dict[str, str]:
    """Apple's item1.X-ABLabel: a standard label in _$!<...>!$_, or the user's own text."""
    labels: dict[str, str] = {}
    for p in card.all("X-ABLABEL"):
        if not p.group:
            continue
        t = reader.text(p)
        if t is None:
            continue
        m = _APPLE_LABEL_RE.fullmatch(t.strip())
        labels[p.group.lower()] = _LABEL_WORDS.get(m.group(1).lower(), "") if m else _line(t, MAX_LABEL_CHARS)
    return labels


def _label(prop: Prop, groups: dict[str, str]) -> str:
    if prop.group and prop.group.lower() in groups:
        return groups[prop.group.lower()]
    words = prop.params.get("TYPE", "").split(",") + [k for k, v in prop.params.items() if v == ""]
    for w in words:
        if w.strip().lower() in _LABEL_WORDS:
            return _LABEL_WORDS[w.strip().lower()]
    return ""


# --- contact cards --------------------------------------------------------------------------

_CONTACT_DROPPED = {
    "URL": "website",
    "PHOTO": "photo",
    "LOGO": "photo",
    "IMPP": "instant messaging",
    "X-AIM": "instant messaging",
    "X-ICQ": "instant messaging",
    "X-JABBER": "instant messaging",
    "X-MSN": "instant messaging",
    "X-YAHOO": "instant messaging",
    "X-SKYPE": "instant messaging",
    "X-GOOGLE-TALK": "instant messaging",
    "X-QQ": "instant messaging",
    "X-GADUGADU": "instant messaging",
    "X-SOCIALPROFILE": "social profile",
}


def _dropped(props: tuple[Prop, ...], table: dict[str, str]) -> tuple[str, ...]:
    return tuple(sorted({table[p.name] for p in props if p.name in table}))


def contact_fields(card: VCard, rules: Rules) -> ContactFields:
    reader = _Reader()
    notes: list[str] = []
    groups = _group_labels(card, reader)

    name = reader.first(card.all("FN"))
    if not name.strip():
        for p in card.all("N")[:1]:
            parts = reader.parts(p)
            if parts:
                family, given, additional, prefix, suffix = (*parts, "", "", "", "", "")[:5]
                name = " ".join(x for x in (prefix, given, additional, family, suffix) if x.strip())
    org = ""
    for p in card.all("ORG")[:1]:
        parts = reader.parts(p)
        org = ", ".join(x for x in parts if x.strip()) if parts else ""
    title = reader.first(card.all("TITLE"))
    nickname = reader.first(card.all("NICKNAME"))

    emails: list[Item] = []
    phones: list[Item] = []
    addresses: list[Item] = []
    rejected = {"phone number(s)": 0, "email address(es)": 0, "address(es)": 0, "postal code(s)": 0}
    for p in card.all("EMAIL"):
        t = reader.text(p)
        if t is None:
            continue
        addr = _email(t)
        if addr is None:
            rejected["email address(es)"] += 1
        else:
            emails.append(Item(_label(p, groups), addr))

    for p in card.all("TEL"):
        if not reader.fits(p):
            continue
        try:
            number = _phone(_ical_unescape(_decode(p, p.value)))
        except _Undecodable:
            reader.failed += 1
            continue
        if number is None:
            rejected["phone number(s)"] += 1
        else:
            phones.append(Item(_label(p, groups), number))

    for p in card.all("ADR"):
        if not reader.fits(p):
            continue
        try:
            raw_parts = tuple(_decode(p, x) for x in p.parts)
            shown = tuple(_normalize(_html(x)) for x in raw_parts)
        except _Undecodable:
            reader.failed += 1
            continue
        if len(raw_parts) > 7:
            rejected["address(es)"] += 1
            continue
        raw_parts = (*raw_parts, *("",) * (7 - len(raw_parts)))
        shown = (*shown, *("",) * (7 - len(shown)))
        text, code_dropped = _address(shown, raw_parts[5])
        rejected["postal code(s)"] += code_dropped
        if text:
            addresses.append(Item(_label(p, groups), text))

    for kind, items in (
        ("phone number(s)", phones),
        ("email address(es)", emails),
        ("address(es)", addresses),
    ):
        rejected[kind] += max(0, len(items) - MAX_ITEMS)
    notes += [f"{n} {kind} not shown" for kind, n in rejected.items() if n]

    birthday = ""
    for p in card.all("BDAY")[:1]:
        t = reader.text(p)
        b = _birthday(t, p.params.get("X-APPLE-OMIT-YEAR", "")) if t is not None else None
        if b is None:
            notes.append("birthday not shown")
        else:
            birthday = b

    hit = bool(rules.keyword_hit(name, org, title, nickname))
    note, withheld, n = reader.free_text("Note", card.all("NOTE"), rules, hit)
    notes += n + reader.notes()
    return ContactFields(
        name=_line(name),
        org=_line(org),
        title=_line(title),
        nickname=_line(nickname),
        emails=tuple(emails[:MAX_ITEMS]),
        phones=tuple(phones[:MAX_ITEMS]),
        addresses=tuple(addresses[:MAX_ITEMS]),
        birthday=birthday,
        note=note,
        note_withheld=withheld,
        dropped=_dropped(card.props, _CONTACT_DROPPED),
        notes=tuple(notes),
    )


# --- events -----------------------------------------------------------------------------------

_EVENT_DROPPED = {
    "ATTACH": "attachment",
    "URL": "website",
    "IMAGE": "image",
    "CONFERENCE": "conference link",
    "X-GOOGLE-CONFERENCE": "conference link",
    "X-MICROSOFT-SKYPETEAMSMEETINGURL": "conference link",
    "X-MICROSOFT-ONLINEMEETINGCONFLINK": "conference link",
}
_PARTSTAT = frozenset({"ACCEPTED", "DECLINED", "TENTATIVE", "NEEDS-ACTION", "DELEGATED"})
_STATUS = frozenset({"CONFIRMED", "TENTATIVE", "CANCELLED"})


def _mailto(value: str) -> str:
    v = _normalize(value).strip()
    return v[7:] if v[:7].lower() == "mailto:" else ""


def _person(prop: Prop) -> tuple[Person, tuple[str, ...]]:
    """(person, the normalized addresses it names, valid or not: for classification only).
    The addresses are the mailto value and the EMAIL parameter (RFC 7986 section 6.2), which
    carries the address when the value is another kind of URI, such as urn:uuid:..."""
    email = _normalize(_param(prop, "EMAIL")).strip()
    addrs = tuple(a for a in (_mailto(prop.value), _mailto(email) or email) if a)
    partstat = _normalize(_param(prop, "PARTSTAT")).strip().upper()
    name = _param(prop, "CN")
    person = Person(
        name=_line(_normalize(_html(name))) if len(name) <= MAX_FIELD_INPUT_CHARS else "",
        address=next((e for e in map(_email, addrs) if e), ""),
        status=partstat.lower() if prop.name == "ATTENDEE" and partstat in _PARTSTAT else "",
    )
    return person, addrs


def event_fields(
    event: VEvent, rules: Rules, *, method: str | None = None, own_addresses: Collection[str] = ()
) -> EventFields:
    """The text fields of one event. method is the calendar object's METHOD; own_addresses
    are the account's addresses, which say whether this is an invitation the user has not
    accepted. An invitation in which none of them is found (METHOD:REQUEST, or an organizer
    who is not the user and an attendee list) is "acceptance unknown"."""
    reader = _Reader()
    notes: list[str] = []
    method_u = _normalize(method or "").strip().upper()
    own = {a.strip().lower() for a in own_addresses}

    title = reader.first(event.all("SUMMARY"))
    location = reader.first(event.all("LOCATION"))

    organizers = [(_person(p), _mailto(_param(p, "SENT-BY"))) for p in event.all("ORGANIZER")]
    blocked = any(
        rules.sender_is_blocked(s) or any(rules.sender_is_blocked(a) for a in addrs)
        for (_, addrs), s in organizers
    )
    organizer = organizers[0][0][0] if organizers else None
    if organizer is not None and not (organizer.name or organizer.address):
        organizer = None

    def is_own(addrs: tuple[str, ...]) -> bool:
        return any(a.lower() in own for a in addrs)

    everyone = [_person(p) for p in event.all("ATTENDEE")]
    people = [(person, addrs) for person, addrs in everyone if person.name or person.address]

    mine = [person for person, addrs in everyone if is_own(addrs)]
    if organizers and is_own(organizers[0][0][1]):
        invitation = ""
    elif mine:
        invitation = "" if mine[0].status == "accepted" else "not accepted"
    elif method_u == "REQUEST" or (organizers and everyone):
        invitation = "acceptance unknown"
    else:
        invitation = ""

    withhold = blocked or bool(rules.keyword_hit(title, location))
    description, withheld, n = reader.free_text("Description", event.all("DESCRIPTION"), rules, withhold)
    notes += n

    status = ""
    for p in event.all("STATUS")[:1]:
        t = (reader.text(p) or "").strip().upper()
        status = t.lower() if t in _STATUS else ""

    notes += reader.notes()
    return EventFields(
        title=_line(title),
        location=_line(location),
        description=description,
        description_withheld=withheld,
        organizer=organizer,
        attendees=tuple(person for person, _ in people[:MAX_ATTENDEES]),
        attendees_omitted=max(0, len(people) - MAX_ATTENDEES),
        status=status,
        cancelled=status == "cancelled" or method_u == "CANCEL",
        invitation=invitation,
        dropped=_dropped(event.props, _EVENT_DROPPED),
        notes=tuple(notes),
    )
