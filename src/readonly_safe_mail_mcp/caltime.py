"""Calendar timing (issue #1, "Calendar semantics", "Recurrence expansion", "Rolling window").

An event's DTSTART, DTEND, DURATION, RRULE, RDATE, EXDATE and RECURRENCE-ID are parsed here
into validated values (a `timing` dict, stored as JSON beside the event's field-policy
text), never kept as raw text. What this code cannot fully understand is kept and marked
incomplete with a reason; such an event is indexed at its first occurrence, and searches
name it (query.py), so it never makes a date search look empty.

Occurrences are derived data: rebuilt from the stored timing with no network request
whenever the window, the machine's zone or EXPANDER_VERSION changes. Repeating events are
expanded in the worker child (expand.py) in batches with deadlines and one time budget per
sync; the rest is pending until the next sync. What bounds an event is the worker's time
limit per event (POSIX) and the wall-clock kill of its child; the caps on the values it
gets do not, since a rule that never matches runs to the year 9999 without yielding.
A worker that gives no usable answer stops the pass: its events stay pending and the
calendar source records the error, rather than each event being blamed for it.

Values in `timing` are tagged strings: "2026-03-02" (a date), "2026-03-02T09:00:00|<zone>"
where <zone> is "UTC", an IANA key, or empty for floating time (read in the machine's zone).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import davindex, isolate
from .config import Account
from .fieldpolicy import EventFields, event_fields
from .vcal import Prop, VEvent, parse_icalendar

log = logging.getLogger(__name__)

# Bump whenever the occurrences built from the same timing could change.
EXPANDER_VERSION = "1"
# Bump whenever event_timing (or the parser under it) could read the same iCalendar text
# differently. It is in the second file's fingerprint, so every calendar item is fetched
# and read again: the raw text is never stored. 2: no start, unknown ends, floating values.
TIMING_VERSION = "2"
EXPAND_WORKER = "readonly_safe_mail_mcp.expand"
BATCH = 200
START_SECONDS = 10.0  # the worker's start, measured once per pass; longer stops the pass
BATCH_SECONDS = 10.0  # a batch's deadline after the worker's start
SINGLE_SECONDS = 2.0  # one event's deadline after the worker's start
EVENT_SECONDS = 1.0  # the worker's own limit per event (POSIX), inside SINGLE_SECONDS
BUDGET_SECONDS = 60.0
WORKER_CPU_SECONDS = 15
MAX_DATES = 2_000  # RDATE values, and EXDATE values, per event
MIN_YEAR = 1900
MAX_DURATION_DAYS = 3_660
PAD_DAYS = 2  # a zone is at most a day from the machine's: expand a little wider, then filter

FLOATING = "(floating)"
TZ_UNKNOWN = "time zone not recognised"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# --- the machine's zone and the window -------------------------------------------------


def local_zone() -> tzinfo:
    """The machine's zone, for floating and all-day times and for every date shown.
    The one place it is read: tests pass their own zone in its place (Expansion.zone)."""
    if sys.platform == "win32":
        from dateutil.tz.win import tzwinlocal

        return tzwinlocal()
    from dateutil import tz

    zone = tz.gettz()
    if zone is None:
        raise RuntimeError("the machine's time zone could not be read")
    return zone


def zone_id(zone: tzinfo) -> str:
    """Changes when the machine's zone does: its UTC offsets in January and July."""
    year = datetime.now(UTC).year
    offsets = [datetime(year, m, 15, 12, tzinfo=UTC).astimezone(zone).utcoffset() for m in (1, 7)]
    return "/".join(str(o) for o in offsets)


@dataclass(frozen=True)
class Window:
    """The days indexed: first day of this month minus `years`, up to (not including) the
    first day of next month plus `years`."""

    years: int
    first: date
    end: date

    @property
    def last(self) -> date:
        return self.end - timedelta(days=1)

    @property
    def label(self) -> str:
        return f"{self.first.isoformat()} to {self.last.isoformat()}"


def window(today: date, years: int) -> Window:
    nxt = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    return Window(years, date(today.year - years, today.month, 1), date(nxt.year + years, nxt.month, 1))


@dataclass(frozen=True)
class Expansion:
    """How a sync expands repeating events. The defaults are production's; tests pass
    shorter deadlines, a worker of their own, a clock, a day or a zone."""

    batch_seconds: float = BATCH_SECONDS
    single_seconds: float = SINGLE_SECONDS
    event_seconds: float = EVENT_SECONDS
    start_seconds: float = START_SECONDS
    budget_seconds: float = BUDGET_SECONDS
    runner: isolate.Runner | None = None  # None: expand.py, sandboxed where a sandbox exists
    today: Callable[[], date] = date.today
    zone: Callable[[], tzinfo] | None = None  # None: local_zone
    clock: Callable[[], float] = time.monotonic


PRODUCTION = Expansion()


# --- parsing: iCalendar properties -> validated, tagged values ---------------------------

_DT_RE = re.compile(r"(\d{4})(\d\d)(\d\d)(?:T(\d\d)(\d\d)(\d\d)(Z?))?", re.A)
_ZONE_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+){0,3}", re.A)
_DURATION_RE = re.compile(
    r"\+?P(?:(\d{1,6})W|(?:(\d{1,7})D)?(?:T(?:(\d{1,8})H)?(?:(\d{1,9})M)?(?:(\d{1,10})S)?)?)", re.A
)


class _Unreadable(ValueError):
    pass


def iana_zone(key: str) -> str | None:
    """`key` if it names a zone in the IANA database, "UTC" for UTC, else None. Embedded
    VTIMEZONE definitions are never parsed."""
    key = key.strip()
    if len(key) > 64 or not _ZONE_KEY_RE.fullmatch(key):
        return None
    try:
        ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
    return "UTC" if key.upper() in ("UTC", "ETC/UTC", "Z") else key


def _tagged(value: str, params: dict[str, str], reasons: set[str]) -> str:
    """One DATE or DATE-TIME value as a tagged string. Raises _Unreadable."""
    m = _DT_RE.fullmatch(value.strip())
    if m is None:
        raise _Unreadable
    y, mo, d, hh, mm, ss, z = m.groups()
    try:
        day = date(int(y), int(mo), int(d))
        if hh is None:
            if params.get("VALUE", "DATE").upper() != "DATE":
                raise _Unreadable
            return day.isoformat()
        wall = datetime(day.year, day.month, day.day, int(hh), int(mm), int(ss))
    except ValueError:
        raise _Unreadable from None
    if params.get("VALUE", "DATE-TIME").upper() != "DATE-TIME":
        raise _Unreadable
    if z:
        zone = "UTC"
    else:
        name = params.get("TZID")
        zone = ""
        if name is not None:
            zone = iana_zone(name) or ""
            if not zone:
                reasons.add(TZ_UNKNOWN)
    return f"{wall.isoformat()}|{zone}"


def _one(props: list[Prop], reasons: set[str], what: str) -> str | None:
    if not props:
        return None
    try:
        return _tagged(props[0].value, props[0].params, reasons)
    except _Unreadable:
        reasons.add(f"{what} could not be read")
        return None


def _duration(value: str) -> tuple[int, int]:
    m = _DURATION_RE.fullmatch(value.strip())
    if m is None or not any(m.groups()):
        raise _Unreadable
    w, d, h, mi, s = (int(x or 0) for x in m.groups())
    days, seconds = 7 * w + d, 3600 * h + 60 * mi + s
    if days + seconds / 86400 > MAX_DURATION_DAYS:
        raise _Unreadable
    return days, seconds


_FREQ_OK = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
_FREQ_FINE = ("SECONDLY", "MINUTELY", "HOURLY")
_WEEKDAY = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_BYDAY_RE = re.compile(r"([+-]?)(\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)", re.A)
_SIGNED = {"BYMONTHDAY": 31, "BYYEARDAY": 366, "BYWEEKNO": 53, "BYSETPOS": 366}
_MAX_LIST = 400


class _BadRule(ValueError):
    """A repeat rule this code reads as malformed; the reason says so."""


def _ints(v: str, lo: int, hi: int, signed: bool) -> list[int]:
    items = v.split(",")
    if len(items) > _MAX_LIST:
        raise _BadRule
    out = []
    for it in items:
        if not re.fullmatch(r"[+-]?\d{1,7}" if signed else r"\d{1,7}", it.strip(), re.A):
            raise _BadRule
        n = int(it)
        if not lo <= abs(n) <= hi:
            raise _BadRule
        out.append(n)
    return out


def _rule(value: str, reasons: set[str]) -> tuple[dict | None, str | None]:
    """(RRULE as the dict expand.py takes, or None with a reason in `reasons`; its UNTIL as
    a tagged value when readable, even when the rule is not supported)."""
    parts: dict[str, str] = {}
    for item in value.strip().split(";"):
        k, sep, v = item.partition("=")
        k = k.strip().upper()
        if not sep or not k or k in parts:
            reasons.add("repeat rule could not be read")
            return None, None
        parts[k] = v.strip()
    until = None
    if "UNTIL" in parts and "COUNT" not in parts:
        try:
            until = _tagged(parts["UNTIL"], {}, set())
        except _Unreadable:
            pass
    freq = parts.get("FREQ", "").upper()
    if freq in _FREQ_FINE:
        reasons.add("repeats more often than daily")
        return None, until
    if {"BYHOUR", "BYMINUTE", "BYSECOND"} & set(parts):
        reasons.add("repeat rule uses BYHOUR, BYMINUTE or BYSECOND")
        return None, until
    try:
        if freq not in _FREQ_OK:
            raise _BadRule
        rule: dict = {"freq": freq}
        for k, v in parts.items():
            if k == "FREQ":
                continue
            if k in ("INTERVAL", "COUNT"):
                [n] = _ints(v, 1, 10_000 if k == "INTERVAL" else 1_000_000, False)
                rule[k.lower()] = n
            elif k == "UNTIL":
                try:
                    rule["until"] = _tagged(v, {}, reasons)
                except _Unreadable:
                    raise _BadRule from None
            elif k == "WKST":
                if v.upper() not in _WEEKDAY:
                    raise _BadRule
                rule["wkst"] = v.upper()
            elif k == "BYMONTH":
                rule["bymonth"] = _ints(v, 1, 12, False)
            elif k in _SIGNED:
                rule[k.lower()] = _ints(v, 1, _SIGNED[k], True)
            elif k == "BYDAY":
                items = v.upper().split(",")
                if len(items) > _MAX_LIST:
                    raise _BadRule
                days = []
                for it in items:
                    m = _BYDAY_RE.fullmatch(it.strip())
                    if m is None:
                        raise _BadRule
                    sign, n, wd = m.groups()
                    if n is not None and not 1 <= int(n) <= 53:
                        raise _BadRule
                    days.append([None if n is None else int(sign + n), wd])
                rule["byday"] = days
            else:
                raise _BadRule
        ordinal = any(n is not None for n, _ in rule.get("byday", []))
        if (
            ("count" in rule and "until" in rule)
            or (ordinal and (freq not in ("MONTHLY", "YEARLY") or "byweekno" in rule))
            or ("bymonthday" in rule and freq == "WEEKLY")
            or ("byyearday" in rule and freq in ("DAILY", "WEEKLY", "MONTHLY"))
            or ("byweekno" in rule and freq != "YEARLY")
            or (
                "bysetpos" in rule
                and not ({"bymonth", "bymonthday", "byyearday", "byweekno", "byday"} & set(rule))
            )
        ):
            raise _BadRule
    except _BadRule:
        reasons.add("repeat rule could not be read")
        return None, None
    return rule, rule.get("until")


def _date_list(props: list[Prop], reasons: set[str], name: str) -> list[str] | None:
    """Every value of every RDATE or EXDATE property; None (with a reason) if any is
    unsupported or there are more than MAX_DATES."""
    out: list[str] = []
    for p in props:
        if p.params.get("VALUE", "").upper() == "PERIOD" or "/" in p.value:
            reasons.add("RDATE periods are not supported")
            return None
        for v in p.value.split(","):
            if len(out) >= MAX_DATES:
                reasons.add(f"more than {MAX_DATES:,} {name} values")
                return None
            try:
                out.append(_tagged(v, p.params, reasons))
            except _Unreadable:
                reasons.add(f"{name} could not be read")
                return None
    return out


def _day(tagged: str) -> date:
    value, _ = _split(tagged)
    return value.date() if isinstance(value, datetime) else value


def event_timing(ev: VEvent) -> dict:
    """The validated timing of one VEVENT: `incomplete` lists what could not be fully
    understood (empty when complete); `repeat` says whether the worker expands it;
    `last_start` is the latest possible start, None when unbounded or unknown (also when
    the end is unknown, so the last day it touches is)."""
    reasons: set[str] = set()
    start = _one(ev.all("DTSTART"), reasons, "start")
    rid_props = ev.all("RECURRENCE-ID")
    if not ev.all("DTSTART") and not rid_props:
        reasons.add("no start")
    rid = _one(rid_props, reasons, "RECURRENCE-ID")
    if start is None and rid is not None and not ev.all("DTSTART"):
        start = rid  # an override without its own DTSTART keeps the original time
    if rid_props and rid_props[0].params.get("RANGE", "").upper() == "THISANDFUTURE":
        reasons.add("RANGE=THISANDFUTURE is not supported")
    all_day = start is not None and "|" not in start
    timing: dict = {"start": start, "end": None, "days": 1 if all_day else 0, "seconds": 0, "rule": None}
    timing.update({"rdates": [], "exdates": [], "recurrence_id": rid})
    if start is not None and _day(start).year < MIN_YEAR:
        reasons.add(f"starts before {MIN_YEAR}")

    end = _one(ev.all("DTEND"), reasons, "end")
    if start is not None and end is not None:
        if ("|" in end) == all_day:
            reasons.add("end could not be read")
        elif all_day:
            timing["days"] = max(1, (date.fromisoformat(end) - date.fromisoformat(start)).days)
        else:
            timing["end"] = end
    elif start is not None and ev.all("DURATION"):
        try:
            days, seconds = _duration(ev.all("DURATION")[0].value)
        except _Unreadable:
            reasons.add("end could not be read")
        else:
            if all_day:
                timing["days"] = max(1, days + -(-seconds // 86400))
            else:
                timing["days"], timing["seconds"] = days, seconds

    rules, rdates = ev.all("RRULE"), ev.all("RDATE")
    expandable = bool(rules or rdates) and start is not None and f"starts before {MIN_YEAR}" not in reasons
    last: list[str] | None = [start] if start is not None else None
    if len(rules) > 1:
        reasons.add("more than one repeat rule")
        expandable, last = False, None
    elif rules:
        rule, until = _rule(rules[0].value, reasons)
        if rule is not None and until is not None and start is not None and not all_day:
            wall, zone = _split(until) if "|" in until else (None, None)
            if zone == "":  # a floating UNTIL is in the start's zone
                rule["until"] = until = f"{wall.isoformat()}|{_split(start)[1]}"
        timing["rule"] = rule
        expandable = expandable and rule is not None
        if last is not None:
            last = [*last, until] if until is not None else None
    if ev.all("EXRULE"):
        reasons.add("EXRULE is not supported")
        expandable = False
    for key, name in (("rdates", "RDATE"), ("exdates", "EXDATE")):
        values = _date_list(ev.all(name), reasons, name)
        if values is None:
            expandable = False
            if key == "rdates":
                last = None
        else:
            timing[key] = values
    if last is not None and timing["rdates"]:
        last = [*last, *timing["rdates"]]
    if {"end could not be read", "RANGE=THISANDFUTURE is not supported"} & reasons:
        last = None  # how far it reaches is unknown: named in every later search
    timing["repeat"] = expandable
    timing["last_start"] = max(last, key=_day) if last else None
    timing["incomplete"] = sorted(reasons)
    return timing


@dataclass(frozen=True)
class ParsedEvent:
    fields: EventFields
    timing: dict


def parse_object(text: str, acct: Account) -> list[ParsedEvent]:
    """Every VEVENT of one calendar object. An override (RECURRENCE-ID) replaces the
    matching occurrence of its master event. Raises on anything the parser refuses."""
    cal = parse_icalendar(text)
    out = []
    by_uid: dict[str, list[ParsedEvent]] = {}
    for ev in cal.events:
        p = ParsedEvent(
            event_fields(ev, acct.rules, method=cal.method, own_addresses=(acct.username,)), event_timing(ev)
        )
        out.append(p)
        uid = ev.all("UID")[0].value.strip() if ev.all("UID") else ""
        by_uid.setdefault(uid, []).append(p)
    for group in by_uid.values():
        masters = [p for p in group if p.timing["recurrence_id"] is None]
        overrides = [p for p in group if p.timing["recurrence_id"] is not None]
        if len(masters) != 1 or not overrides:
            continue
        master = masters[0].timing
        master["exdates"] = master["exdates"] + [p.timing["recurrence_id"] for p in overrides]
        if any("RANGE=THISANDFUTURE is not supported" in p.timing["incomplete"] for p in overrides):
            master["incomplete"] = sorted({*master["incomplete"], "RANGE=THISANDFUTURE is not supported"})
            master["repeat"] = False
    return out


# --- occurrences ---------------------------------------------------------------------------


def _zone(key: str, local: tzinfo) -> tzinfo:
    if key == "":
        return local
    return UTC if key == "UTC" else ZoneInfo(key)


def _split(tagged: str) -> tuple[datetime, str] | tuple[date, None]:
    if "|" not in tagged:
        return date.fromisoformat(tagged), None
    wall, zone = tagged.split("|")
    return datetime.fromisoformat(wall), zone


def describe(tagged: str) -> str:
    """A tagged value for people: "2027-01-01", "2027-01-01 09:00 Europe/Paris", "... (floating)"."""
    value, zone = _split(tagged)
    if zone is None:
        return value.isoformat()
    return f"{value.strftime('%Y-%m-%d %H:%M')} {zone or FLOATING}"


def describe_rule(rule: dict) -> str:
    """A validated repeat rule in RRULE syntax, rendered by this code from its values."""
    parts = [f"FREQ={rule['freq']}"]
    for key in ("interval", "count", "bymonth", "bymonthday", "byyearday", "byweekno", "bysetpos"):
        if key in rule:
            v = rule[key]
            parts.append(f"{key.upper()}={','.join(map(str, v)) if isinstance(v, list) else v}")
    if "byday" in rule:
        parts.append("BYDAY=" + ",".join(f"{'' if n is None else n}{d}" for n, d in rule["byday"]))
    if "wkst" in rule:
        parts.append(f"WKST={rule['wkst']}")
    if "until" in rule:
        parts.append(f"UNTIL={describe(rule['until'])}")
    return ";".join(parts)


def _wall_in(tagged: str, zone_key: str | None, start_wall: datetime | date, local: tzinfo) -> str:
    """A tagged value as a wall-clock ISO string in the start's zone (zone_key None: the
    start is a date)."""
    value, vzone = _split(tagged)
    if zone_key is None:  # all-day event: dates only
        return (value.date() if isinstance(value, datetime) else value).isoformat()
    if vzone is None:  # a date in a timed event: the start's time of day that date
        assert isinstance(start_wall, datetime)
        return datetime.combine(value, start_wall.time()).isoformat()
    if vzone and vzone != zone_key:  # a floating value is in the start's zone, as UNTIL is
        value = (
            value.replace(tzinfo=_zone(vzone, local)).astimezone(_zone(zone_key, local)).replace(tzinfo=None)
        )
    return value.isoformat()


def _until_in(tagged: str, zone_key: str | None, start_wall: datetime | date, local: tzinfo) -> str:
    """UNTIL, an instant: on an all-day event, the last day whose midnight (the machine's,
    where all-day days are placed) is not after it."""
    value, vzone = _split(tagged)
    if zone_key is None and vzone is not None:
        assert isinstance(value, datetime)
        return value.replace(tzinfo=_zone(vzone, local)).astimezone(local).date().isoformat()
    return _wall_in(tagged, zone_key, start_wall, local)


def _exdate_in(tagged: str, zone_key: str | None, start_wall, local: tzinfo) -> str:
    value, vzone = _split(tagged)
    if zone_key is not None and vzone is None:
        return value.isoformat()  # EXDATE;VALUE=DATE on a timed event: that whole day
    return _wall_in(tagged, zone_key, start_wall, local)


def _instant(wall: datetime, zone: tzinfo) -> float:
    return wall.replace(tzinfo=zone).timestamp()


def _local(ts: float, local: tzinfo) -> datetime:
    return (_EPOCH + timedelta(seconds=ts)).astimezone(local)


@dataclass(frozen=True)
class Occurrence:
    start: float
    first_day: int
    last_day: int
    label: str


@dataclass(frozen=True)
class Built:
    occurrences: list[Occurrence]
    first_label: str
    maybe_first: int | None
    maybe_last: int | None


def _span(wall, zone_key: str | None, timing: dict, end_offset: float | None, local: tzinfo) -> Occurrence:
    if zone_key is None:
        day = wall if isinstance(wall, date) and not isinstance(wall, datetime) else wall.date()
        last = day + timedelta(days=timing["days"] - 1)
        label = day.isoformat() + (f" – {last.isoformat()}" if last != day else "") + " (all day)"
        start = _instant(datetime.combine(day, datetime.min.time()), local)
        return Occurrence(start, day.toordinal(), last.toordinal(), label)
    zone = _zone(zone_key, local)
    start = _instant(wall, zone)
    if end_offset is not None:
        end = start + end_offset
    else:
        end = _instant(wall + timedelta(days=timing["days"]), zone) + timing["seconds"]
    a, b = _local(start, local), _local(max(start, end), local)
    last = _local(end - 1, local).date() if end > start else a.date()
    label = a.strftime("%Y-%m-%d %H:%M")
    if end > start:
        label += f"–{b.strftime('%H:%M')}" if b.date() == a.date() else f" – {b.strftime('%Y-%m-%d %H:%M')}"
    if zone_key == "":
        label += f" {FLOATING}"
    return Occurrence(start, a.date().toordinal(), last.toordinal(), label)


def build(timing: dict, starts: list[str] | None, win: Window, local: tzinfo) -> Built:
    """The occurrences in `win` of one event: its worker `starts`, or (None) its first
    occurrence only. Also the days an incomplete event could touch (None: unbounded)."""
    if timing["start"] is None:
        return Built([], "start unknown", None, None)
    first_wall, zone_key = _split(timing["start"])
    end_offset = None
    if timing["end"] is not None:
        end_wall, end_zone = _split(timing["end"])
        offset = _instant(end_wall, _zone(end_zone, local)) - _instant(first_wall, _zone(zone_key, local))
        end_offset = max(0.0, offset)
    first = _span(first_wall, zone_key, timing, end_offset, local)
    if starts is None:
        walls = [first_wall]
    else:
        walls = [date.fromisoformat(s) if zone_key is None else datetime.fromisoformat(s) for s in starts]
    lo, hi = win.first.toordinal(), win.end.toordinal()
    spans = (_span(w, zone_key, timing, end_offset, local) for w in walls)
    occ = [o for o in spans if o.last_day >= lo and o.first_day < hi]
    maybe_last = None
    if timing["last_start"] is not None:
        maybe_last = _day(timing["last_start"]).toordinal() + (first.last_day - first.first_day) + 1
    return Built(occ, first.label, first.first_day - 1, maybe_last)


_NO_DATES = Built([], "dates could not be computed", None, None)


def _store(
    db: sqlite3.Connection, eid: int, timing: dict, starts, win: Window, local, key: str, problem: str
) -> None:
    """Store one event's occurrences. Dates that cannot be computed (an overflow near the
    year 9999, a zone missing from the time-zone database, say) mark that event alone
    incomplete, named in every search."""
    try:
        built = build(timing, starts, win, local)
    except Exception as e:  # this event only: the pass goes on
        log.warning("one event's dates could not be computed (%s)", type(e).__name__, exc_info=True)
        built, problem = _NO_DATES, problem or "dates could not be computed"
    davindex.store_occurrences(db, eid, built, key, problem)


def worker_request(eid: int, timing: dict, win: Window, local: tzinfo) -> dict:
    """The validated values expand.py takes for one repeating event."""
    start_wall, zone_key = _split(timing["start"])
    dur_days = timing["days"] + -(-timing["seconds"] // 86400)
    if timing["end"] is not None:
        end_wall, _ = _split(timing["end"])
        dur_days = max(0, (end_wall - start_wall).days + 1)
    after = datetime.combine(win.first - timedelta(days=PAD_DAYS + dur_days), datetime.min.time())
    before = datetime.combine(win.end + timedelta(days=PAD_DAYS), datetime.min.time())
    rule = dict(timing["rule"]) if timing["rule"] else None
    if rule and "until" in rule:
        rule["until"] = _until_in(rule["until"], zone_key, start_wall, local)
    return {
        "id": eid,
        "start": start_wall.isoformat(),
        "rule": rule,
        "rdates": [_wall_in(v, zone_key, start_wall, local) for v in timing["rdates"]],
        "exdates": [_exdate_in(v, zone_key, start_wall, local) for v in timing["exdates"]],
        "after": after.isoformat(),
        "before": before.isoformat(),
        "max": (before - after).days + 1,
    }


def _check_starts(starts: object, req: dict) -> list[str]:
    """The worker's answer for one event, checked before it is used."""
    if not isinstance(starts, list) or len(starts) > req["max"]:
        raise ValueError("bad worker answer")
    lo, hi = req["after"][: len(req["start"])], req["before"][: len(req["start"])]
    for s in starts:
        if not isinstance(s, str) or len(s) != len(req["start"]) or not lo <= s < hi:
            raise ValueError("bad worker answer")
        (date if len(s) == 10 else datetime).fromisoformat(s)
    return starts


# --- the expansion pass ------------------------------------------------------------------


@dataclass
class Progress:
    window: Window
    key: str
    expanded: int = 0
    pending: int = 0
    stopped: str | None = None  # why the pass stopped with the worker giving no usable answer


class _RequestFailed(Exception):
    """The worker answered that the request as a whole failed ("ok": false)."""


def _ask(
    runner: isolate.Runner, reqs: list[dict], seconds: float, event_seconds: float
) -> dict[int, list[str] | str]:
    """{event id: starts, or the reason it could not be expanded}. Raises WorkerError when
    the worker gives no usable answer, _RequestFailed when it says the request failed."""
    request = {"events": reqs, "event_seconds": event_seconds}
    reply = isolate.call_worker(
        runner, json.dumps(request).encode(), timeout=seconds, cpu_seconds=WORKER_CPU_SECONDS
    )
    if not reply["ok"]:
        raise _RequestFailed
    results = reply.get("results")
    if not isinstance(results, list):
        raise isolate.WorkerError("expansion answer malformed")
    by_req = {r["id"]: r for r in reqs}
    out: dict[int, list[str] | str] = {}
    for res in results:
        eid = res.get("id") if isinstance(res, dict) else None
        if eid not in by_req or eid in out:
            raise isolate.WorkerError("expansion answer names an event that was not asked for")
        if isinstance(res.get("error"), str):
            out[eid] = res["error"][:100]
            continue
        try:
            out[eid] = _check_starts(res.get("starts"), by_req[eid])
        except ValueError:
            out[eid] = "repeat rule could not be expanded"
    if set(out) != set(by_req):
        raise isolate.WorkerError("expansion answer incomplete")
    return out


def _stop(progress: Progress, why: object) -> None:
    progress.stopped = f"calendar expansion stopped (worker: {why}); repeating events wait for the next sync"
    log.warning("%s", progress.stopped)


def _expand_repeating(
    db: sqlite3.Connection,
    repeating: list,
    win: Window,
    local: tzinfo,
    key: str,
    exp: Expansion,
    progress: Progress,
) -> None:
    """Repeating events through the worker, within exp's budget. An event is blamed only
    for what the worker said about it, or for a lone retry of it killed at its deadline;
    the worker's start is measured first and not counted against any event."""
    runner = exp.runner or isolate.find_runner("ask-unsandboxed", EXPAND_WORKER)
    started = exp.clock()

    def fits(seconds: float) -> bool:
        return exp.clock() - started + seconds <= exp.budget_seconds

    t0 = time.monotonic()
    try:
        _ask(runner, [], exp.start_seconds, exp.event_seconds)
    except isolate.WorkerError as e:
        return _stop(progress, e)
    except _RequestFailed:
        return _stop(progress, "refused an empty request")
    start_cost = time.monotonic() - t0
    for i in range(0, len(repeating), BATCH):
        batch = repeating[i : i + BATCH]
        if not fits(start_cost + exp.batch_seconds):
            break
        reqs, answers = {}, {}
        for eid, t in batch:
            try:
                reqs[eid] = worker_request(eid, t, win, local)
            except Exception as e:  # this event only: the pass goes on
                log.warning("one event's dates could not be computed (%s)", type(e).__name__, exc_info=True)
                answers[eid] = "dates could not be computed"
        out_of_time = False
        try:
            answers.update(
                _ask(runner, list(reqs.values()), start_cost + exp.batch_seconds, exp.event_seconds)
                if reqs
                else {}
            )
        except (isolate.WorkerError, _RequestFailed):
            for eid in reqs:
                if not fits(start_cost + exp.single_seconds):
                    out_of_time = True
                    break
                try:
                    answers.update(
                        _ask(runner, [reqs[eid]], start_cost + exp.single_seconds, exp.event_seconds)
                    )
                except isolate.WorkerTimeout:
                    answers[eid] = "repeat rule took too long to expand"
                except _RequestFailed:
                    answers[eid] = "repeat rule could not be expanded"
                except isolate.WorkerError as e:
                    _stop(progress, e)
                    break
        for eid, timing in batch:
            if eid not in answers:
                continue
            a = answers[eid]
            starts, problem = (a, "") if isinstance(a, list) else (None, a)
            _store(db, eid, timing, starts, win, local, key, problem)
            progress.expanded += 1
        db.commit()
        if out_of_time or progress.stopped:
            break


def expand_pending(db: sqlite3.Connection, years: int, exp: Expansion) -> Progress:
    """Build the occurrences of every event not yet built for the current window, zone and
    EXPANDER_VERSION. Repeating events go through the worker under exp's deadlines and
    budget; what the budget leaves is pending until the next call."""
    local = (exp.zone or local_zone)()
    win = window(exp.today(), years)
    key = f"{EXPANDER_VERSION}|{win.first}|{win.end}|{zone_id(local)}"
    davindex.set_window(db, win.years, win.first, win.end, key)
    progress = Progress(win, key)
    repeating = []
    for eid, timing_json in davindex.pending_events(db, key):
        timing = json.loads(timing_json)
        if timing["repeat"]:
            repeating.append((eid, timing))
        else:
            _store(db, eid, timing, None, win, local, key, "")
            progress.expanded += 1
    db.commit()
    if repeating:
        _expand_repeating(db, repeating, win, local, key, exp, progress)
    progress.pending = davindex.count_pending(db, key)
    return progress
