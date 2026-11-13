"""vCard / iCalendar line helpers and structured parsers, shared by attachment rendering
(extract.py, which runs inside the attachment sandbox) and the contacts/calendar sync.

Standard library only; reads no config, no environment and no network, so it imports in the
sandbox child as it is. The structured parsers return raw parsed values: no sanitizing, no
time-zone resolution, no recurrence expansion. Input over a limit raises VParseError; there
is never a partial result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_INPUT_CHARS = 5_000_000
MAX_LINES = 20_000
MAX_PROPS = 5_000
MAX_EVENTS = 5_000
MAX_DEPTH = 8


class VParseError(ValueError):
    pass


# --- line helpers ---------------------------------------------------------------------


def _unfold(text: str) -> list[str]:
    lines: list[list[str]] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1].append(raw[1:])
        else:
            lines.append([raw])
    return ["".join(parts) for parts in lines]


def _split_unquoted(s: str, sep: str) -> list[str]:
    out: list[str] = []
    start = 0
    quoted = False
    for i, ch in enumerate(s):
        if ch == '"':
            quoted = not quoted
        elif ch == sep and not quoted:
            out.append(s[start:i])
            start = i + 1
    out.append(s[start:])
    return out


def _split_head(line: str) -> tuple[str, str] | None:
    """Split at the first ':' outside a double-quoted parameter value; None if there is none."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            return line[:i], line[i + 1 :]
    return None


def _param_value(v: str) -> str:
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"' and '"' not in v[1:-1]:
        return v[1:-1]
    return v


def _parse_line(line: str) -> tuple[str, dict[str, str], str, str | None] | None:
    split = _split_head(line)
    if split is None:
        return None
    head, value = split
    name, *params = _split_unquoted(head, ";")
    pd: dict[str, str] = {}
    for p in params:
        k, _, v = p.partition("=")
        k = k.upper()
        v = _param_value(v)
        pd[k] = f"{pd[k]},{v}" if k in pd else v  # TYPE=work;TYPE=voice -> work,voice
    group, _, name = name.rpartition(".")  # item1.EMAIL -> EMAIL
    return name.upper(), pd, value, group or None


def _prop(line: str) -> tuple[str, dict[str, str], str]:
    parsed = _parse_line(line)
    if parsed is None:
        return line.split(";", 1)[0].rsplit(".", 1)[-1].upper(), {}, ""
    return parsed[:3]


_ESCAPES = {"n": "\n", "N": "\n", ",": ",", ";": ";", "\\": "\\"}


def _ical_unescape(v: str) -> str:
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(0)), v, flags=re.S)


def _split_components(value: str) -> tuple[str, ...]:
    """Split a structured value (N, ADR, ORG) at unescaped ';' and unescape each part."""
    parts: list[str] = []
    cur: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            cur.append(value[i : i + 2])
            i += 2
            continue
        if ch == ";":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return tuple(_ical_unescape(p) for p in parts)


def _ics_time(v: str) -> str:
    m = re.match(r"^(\d{4})(\d\d)(\d\d)(?:T(\d\d)(\d\d)(\d\d)?(Z)?)?$", v.strip())
    if not m:
        return v
    y, mo, d, hh, mm, _, z = m.groups()
    s = f"{y}-{mo}-{d}"
    if hh:
        s += f" {hh}:{mm}" + (" UTC" if z else "")
    return s


# --- structured parsers ---------------------------------------------------------------


@dataclass(frozen=True)
class Prop:
    name: str  # upper case, group prefix removed
    params: dict[str, str]  # upper-case names; values un-quoted, repeats joined with ","
    value: str  # raw, still escaped
    group: str | None = None
    parts: tuple[str, ...] = ()  # N, ADR, ORG: unescaped components; empty otherwise


@dataclass(frozen=True)
class VCard:
    version: str
    props: tuple[Prop, ...]

    def all(self, name: str) -> list[Prop]:
        return [p for p in self.props if p.name == name]


@dataclass(frozen=True)
class VEvent:
    props: tuple[Prop, ...]

    def all(self, name: str) -> list[Prop]:
        return [p for p in self.props if p.name == name]


@dataclass(frozen=True)
class VCalendar:
    method: str | None
    has_timezone: bool
    events: tuple[VEvent, ...]


_STRUCTURED = {"N", "ADR", "ORG"}


def _lines(text: str) -> list[tuple[str, dict[str, str], str, str | None]]:
    if len(text) > MAX_INPUT_CHARS:
        raise VParseError("input too large")
    out: list[tuple[str, dict[str, str], str, str | None]] = []
    for raw in _unfold(text):
        if not raw.strip():
            continue
        if len(out) >= MAX_LINES:
            raise VParseError("too many lines")
        parsed = _parse_line(raw)
        if parsed is None:
            raise VParseError("line without a ':'")
        out.append(parsed)
    return out


def _is(line: tuple[str, dict[str, str], str, str | None], name: str, value: str) -> bool:
    return line[0] == name and line[2].strip().upper() == value


def _make_prop(name: str, params: dict[str, str], value: str, group: str | None) -> Prop:
    parts = _split_components(value) if name in _STRUCTURED else ()
    return Prop(name, params, value, group, parts)


def parse_vcard(text: str) -> VCard:
    """Parse exactly one vCard (2.1, 3.0 or 4.0)."""
    lines = _lines(text)
    if len(lines) < 2:
        raise VParseError("not a vCard")
    if not _is(lines[0], "BEGIN", "VCARD") or not _is(lines[-1], "END", "VCARD"):
        raise VParseError("not a single vCard")
    props: list[Prop] = []
    for name, params, value, group in lines[1:-1]:
        if name in ("BEGIN", "END"):
            raise VParseError("nested or repeated vCard")
        if len(props) >= MAX_PROPS:
            raise VParseError("too many properties")
        props.append(_make_prop(name, params, value, group))
    version = next((p.value for p in props if p.name == "VERSION"), "")
    return VCard(version, tuple(props))


def parse_icalendar(text: str) -> VCalendar:
    """Parse one iCalendar object into its VEVENTs.

    VTODO, VJOURNAL and other components are skipped; a VTIMEZONE is only recorded. Alarms
    and other components nested in an event are skipped too.
    """
    lines = _lines(text)
    if not lines or not _is(lines[0], "BEGIN", "VCALENDAR"):
        raise VParseError("not an iCalendar object")
    stack: list[str] = []
    method: str | None = None
    has_tz = False
    events: list[VEvent] = []
    cur: list[Prop] | None = None
    nprops = 0
    closed = False
    for name, params, value, group in lines:
        if closed:
            raise VParseError("data after END:VCALENDAR")
        if name == "BEGIN":
            comp = value.strip().upper()
            if len(stack) >= MAX_DEPTH:
                raise VParseError("components nested too deeply")
            if not stack and comp != "VCALENDAR":
                raise VParseError("not an iCalendar object")
            if stack == ["VCALENDAR"]:
                if comp == "VTIMEZONE":
                    has_tz = True
                elif comp == "VEVENT":
                    if len(events) >= MAX_EVENTS:
                        raise VParseError("too many events")
                    cur = []
            stack.append(comp)
        elif name == "END":
            comp = value.strip().upper()
            if not stack or stack[-1] != comp:
                raise VParseError("unbalanced END")
            stack.pop()
            if stack == ["VCALENDAR"] and comp == "VEVENT" and cur is not None:
                events.append(VEvent(tuple(cur)))
                cur = None
            elif not stack:
                closed = True
        else:
            nprops += 1
            if nprops > MAX_PROPS:
                raise VParseError("too many properties")
            if stack == ["VCALENDAR"] and name == "METHOD":
                method = value.strip()
            elif stack == ["VCALENDAR", "VEVENT"] and cur is not None:
                cur.append(_make_prop(name, params, value, group))
    if not closed:
        raise VParseError("missing END:VCALENDAR")
    return VCalendar(method, has_tz, tuple(events))
