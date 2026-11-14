"""Recurrence expansion worker (issue #1, "Recurrence expansion: bounded and cancellable").

Runs as a short-lived child process, started by isolate.py in the platform sandbox where
there is one, else as a plain child with the same limits:
  python -I -m readonly_safe_mail_mcp.expand  < request
Calendar content is writable by strangers, so this process gets validated values only,
never iCalendar text: wall-clock times as ISO strings, and repeat rules as a dict of
enumerations and integers built by caltime.py. Request on stdin, one JSON object:
  {"events": [{"id", "start", "rule", "rdates", "exdates", "after", "before", "max"}, ...],
   "event_seconds": 1.0}
Reply on stdout:
  {"ok": true, "results": [{"id", "starts": [...]} | {"id", "error": "..."}]}
  {"ok": false, "error": "..."}
Each event's starts are its occurrences in [after, before), in the event's own wall-clock
time; the parent converts them to instants.

What bounds the work: a wall-clock limit per event (`event_seconds`, SIGALRM, POSIX only),
after which that event alone is answered "took too long" and the next one starts; and
the parent's kill of this process at its deadline, the only bound on Windows. The caps on
the values (start of 1900 or later, FREQ daily or coarser, UNTIL clamped to `before`, at
most MAX_STEPS occurrences looked at and `max` kept) do not bound it alone: dateutil
looks for the next match without yielding, so a rule that never matches
(FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30) runs to the year 9999 inside one step.
"""

from __future__ import annotations

import json
import signal
import sys
from datetime import date, datetime

from dateutil import rrule

MAX_INPUT_BYTES = 20_000_000
MAX_EVENTS = 200
MAX_STEPS = 100_000
EVENT_SECONDS = 1.0
MAX_DATES = 10_000  # RDATE, EXDATE and replaced occurrences; caltime.py allows 2,000 of each property
MAX_LIST = 400
MIN_YEAR = 1900

FREQS = {"DAILY": rrule.DAILY, "WEEKLY": rrule.WEEKLY, "MONTHLY": rrule.MONTHLY, "YEARLY": rrule.YEARLY}
WEEKDAYS = {
    "MO": rrule.MO,
    "TU": rrule.TU,
    "WE": rrule.WE,
    "TH": rrule.TH,
    "FR": rrule.FR,
    "SA": rrule.SA,
    "SU": rrule.SU,
}
# rule part -> largest absolute value; a negative value counts from the end, 0 is never allowed
SIGNED_PARTS = {"bymonthday": 31, "byyearday": 366, "byweekno": 53, "bysetpos": 366}
RULE_KEYS = {"freq", "interval", "count", "until", "bymonth", "byday", "wkst", *SIGNED_PARTS}


class Refused(ValueError):
    pass


class _TooLong(Exception):  # not a ValueError: only the per-event limit raises it
    pass


def _too_long(signum, frame):
    raise _TooLong


def _int(v: object, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise Refused("value out of range")
    return v


def _signed(v: object, hi: int) -> int:
    """A rule value from 1 to hi, or from -hi to -1 (counted from the end)."""
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= abs(v) <= hi:
        raise Refused("value out of range")
    return v


def _list(v: object) -> list:
    if not isinstance(v, list) or len(v) > MAX_LIST:
        raise Refused("list too long")
    return v


def _when(v: object, all_day: bool) -> datetime:
    if not isinstance(v, str) or len(v) > 19:
        raise Refused("a date the expander cannot use")
    t = datetime.combine(date.fromisoformat(v), datetime.min.time()) if all_day else datetime.fromisoformat(v)
    if t.tzinfo is not None or t.year < MIN_YEAR:
        raise Refused("a date the expander cannot use")
    return t


def _rule(r: dict, start: datetime, before: datetime, all_day: bool) -> rrule.rrule:
    if not isinstance(r, dict) or r.get("freq") not in FREQS:
        raise Refused("unsupported frequency")
    if not set(r) <= RULE_KEYS:
        raise Refused("unsupported rule part")
    kw: dict = {"dtstart": start, "interval": _int(r.get("interval", 1), 1, 10_000), "cache": False}
    if r.get("count") is not None:
        kw["count"] = _int(r["count"], 1, 1_000_000)
    until = _when(r["until"], all_day) if r.get("until") is not None else before
    if "count" not in kw:
        kw["until"] = min(until, before)
    for part, hi in SIGNED_PARTS.items():
        vals = [_signed(v, hi) for v in _list(r.get(part, []))]
        if vals:
            kw[part] = vals
    months = [_int(v, 1, 12) for v in _list(r.get("bymonth", []))]
    if months:
        kw["bymonth"] = months
    days = []
    for item in _list(r.get("byday", [])):
        if not isinstance(item, list) or len(item) != 2 or item[1] not in WEEKDAYS:
            raise Refused("bad weekday")
        n = item[0]
        days.append(WEEKDAYS[item[1]] if n is None else WEEKDAYS[item[1]](_signed(n, 53)))
    if days:
        kw["byweekday"] = days
    if r.get("wkst") is not None:
        if r["wkst"] not in WEEKDAYS:
            raise Refused("bad week start")
        kw["wkst"] = WEEKDAYS[r["wkst"]]
    return rrule.rrule(FREQS[r["freq"]], **kw)


def expand_one(ev: dict, max_steps: int = MAX_STEPS) -> list[str]:
    """The occurrence starts of one validated event in [after, before), as ISO strings."""
    start_s = ev.get("start")
    all_day = isinstance(start_s, str) and len(start_s) == 10
    start = _when(start_s, all_day)
    after, before = _when(ev.get("after"), False), _when(ev.get("before"), False)
    limit = _int(ev.get("max"), 1, 100_000)
    rdates, exdates = ev.get("rdates", []), ev.get("exdates", [])
    if not isinstance(rdates, list) or not isinstance(exdates, list):
        raise Refused("bad dates")
    if len(rdates) > MAX_DATES or len(exdates) > MAX_DATES:
        raise Refused("too many dates")
    rs = rrule.rruleset(cache=False)
    if ev.get("rule") is not None:
        rs.rrule(_rule(ev["rule"], start, before, all_day))
    rs.rdate(start)
    for v in rdates:
        rs.rdate(_when(v, all_day))
    ex_times, ex_days = set(), set()
    for v in exdates:
        if isinstance(v, str) and len(v) == 10 and not all_day:
            ex_days.add(date.fromisoformat(v))
        else:
            ex_times.add(_when(v, all_day))
    out: list[str] = []
    for steps, occ in enumerate(rs, 1):
        if steps > max_steps:
            raise Refused("repeat rule needs too many steps")
        if occ >= before:
            break
        if occ < after or occ in ex_times or occ.date() in ex_days:
            continue
        if len(out) >= limit:
            raise Refused("more than one occurrence a day")
        out.append(occ.date().isoformat() if all_day else occ.isoformat())
    return out


def expand(request: dict) -> list[dict]:
    events = request.get("events")
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise Refused("bad request")
    seconds = request.get("event_seconds", EVENT_SECONDS)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds <= 60:
        raise Refused("bad request")
    timer = hasattr(signal, "setitimer")  # POSIX; on Windows only the parent's kill bounds an event
    old = signal.signal(signal.SIGALRM, _too_long) if timer else None
    results = []
    try:
        for ev in events:
            eid = ev.get("id") if isinstance(ev, dict) else None
            if isinstance(eid, bool) or not isinstance(eid, int):
                raise Refused("bad request")
            try:
                if timer:
                    signal.setitimer(signal.ITIMER_REAL, seconds)
                try:
                    result = {"id": eid, "starts": expand_one(ev)}
                finally:
                    if timer:
                        signal.setitimer(signal.ITIMER_REAL, 0)
            except _TooLong:
                result = {"id": eid, "error": "repeat rule took too long to expand"}
            except Refused as e:
                result = {"id": eid, "error": str(e)}
            except (ValueError, OverflowError, TypeError, KeyError) as e:
                result = {"id": eid, "error": f"repeat rule could not be expanded ({type(e).__name__})"}
            results.append(result)
    finally:
        if timer:
            signal.signal(signal.SIGALRM, old)
    return results


def _limit_resources() -> None:
    try:
        import resource
    except ImportError:
        return
    for lim, val in (
        (resource.RLIMIT_CPU, 20),
        (resource.RLIMIT_FSIZE, 0),
        (resource.RLIMIT_NOFILE, 64),
        (resource.RLIMIT_AS, 1_000_000_000),  # enforced on Linux; macOS ignores it
    ):
        try:
            resource.setrlimit(lim, (val, val))
        except (ValueError, OSError):
            pass


def main() -> int:
    _limit_resources()
    out = sys.stdout
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise Refused("request too large")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise Refused("bad request")
        json.dump({"ok": True, "results": expand(request)}, out)
    except Refused as e:
        json.dump({"ok": False, "error": str(e)}, out)
    except MemoryError:
        json.dump({"ok": False, "error": "expansion needs too much memory"}, out)
    except Exception as e:  # never echo input; only the error type
        json.dump({"ok": False, "error": f"expansion failed ({type(e).__name__})"}, out)
    out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
