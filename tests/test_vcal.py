import pytest

from readonly_safe_mail_mcp import vcal
from readonly_safe_mail_mcp.extract import extract_ics, extract_vcf
from readonly_safe_mail_mcp.vcal import (
    VParseError,
    _ical_unescape,
    _ics_time,
    _prop,
    _unfold,
    parse_icalendar,
    parse_vcard,
)

CARD = r"""BEGIN:VCARD
VERSION:3.0
N:Doe;Jane;Q;Dr.;Jr.
FN:Jane Q. Doe
ORG:Example Co;Sales\; West
TITLE:Buyer
NICKNAME:JD
NOTE:Line one\nLine two
item1.EMAIL;TYPE=INTERNET;TYPE=work:jane@example.org
TEL;TYPE=cell:+1 555 0100
ADR;TYPE=home:;;1 Example St\; Apt 2;Springfield;ST;00000;Exampleland
BDAY:1990-01-02
URL:https://example.org
PHOTO;ENCODING=b;TYPE=JPEG:AAAA
IMPP;X-SERVICE-TYPE=Example:example:jane
X-SOCIALPROFILE;type=example:https://example.org/jane
END:VCARD
"""

ICS = """BEGIN:VCALENDAR
VERSION:2.0
METHOD:REQUEST
BEGIN:VTIMEZONE
TZID:Example/Zone
BEGIN:STANDARD
DTSTART:19700101T000000
TZOFFSETFROM:+0000
TZOFFSETTO:+0000
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:one@example.org
DTSTART;TZID=Europe/Paris:20260303T090000
DURATION:PT1H
SUMMARY:Standup
RRULE:FREQ=WEEKLY;BYDAY=TU
EXDATE;TZID=Europe/Paris:20260310T090000
RDATE;VALUE=DATE:20260401
STATUS:CONFIRMED
ORGANIZER;CN="Boss: The":mailto:boss@example.org
ATTENDEE;CN=Pat;PARTSTAT=ACCEPTED;RSVP=TRUE:mailto:pat@example.org
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT10M
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:one@example.org
RECURRENCE-ID;TZID=Europe/Paris:20260317T090000
DTSTART;TZID=Europe/Paris:20260317T100000
SUMMARY:Standup moved
END:VEVENT
BEGIN:VTODO
UID:todo@example.org
SUMMARY:Not an event
END:VTODO
END:VCALENDAR
"""


# --- the two fixed defects ---


def test_prop_colon_inside_quoted_param():
    name, params, value = _prop('ATTENDEE;CN="Smith: Bob";PARTSTAT=ACCEPTED:mailto:bob@example.org')
    assert name == "ATTENDEE"
    assert params == {"CN": "Smith: Bob", "PARTSTAT": "ACCEPTED"}
    assert value == "mailto:bob@example.org"


def test_prop_semicolon_inside_quoted_param():
    _, params, value = _prop('X;CN="A; B";Y=1:v')
    assert params == {"CN": "A; B", "Y": "1"}
    assert value == "v"


def test_attachment_rendering_keeps_quoted_colon_name():
    text = 'BEGIN:VEVENT\nORGANIZER;CN="Smith: Bob":mailto:bob@example.org\nEND:VEVENT\n'
    out = extract_ics(text.encode(), "utf-8")
    assert "Organizer: Smith: Bob <bob@example.org>" in out


def test_unescape_backslash_then_n_is_not_a_newline():
    assert _ical_unescape("C:\\\\new") == "C:\\new"
    assert _ical_unescape("a\\\\nb") == "a\\nb"


def test_unescape_ordinary_escapes():
    assert _ical_unescape(r"a\nb\Nc\,d\;e\\f") == "a\nb\nc,d;e\\f"
    assert _ical_unescape("\\x trailing\\") == "\\x trailing\\"


# --- helpers ---


def test_prop_other_cases():
    assert _prop("item1.EMAIL;type=work:a@example.org") == (
        "EMAIL",
        {"TYPE": "work"},
        "a@example.org",
    )
    assert _prop("TEL;TYPE=a;TYPE=b:1") == ("TEL", {"TYPE": "a,b"}, "1")
    assert _prop("TEL;WORK;VOICE:1")[1] == {"WORK": "", "VOICE": ""}
    assert _prop("no colon here") == ("NO COLON HERE", {}, "")
    assert _prop('X;CN="open:v') == ("X", {}, "")  # unterminated quote: no value


def test_unfold_and_time():
    assert _unfold("A:1\r\n 2\r\n\tx\r\nB:3") == ["A:12x", "B:3"]
    assert _ics_time("20260303T090000Z") == "2026-03-03 09:00 UTC"
    assert _ics_time("20260303") == "2026-03-03"
    assert _ics_time("junk") == "junk"


def test_attachment_rendering_unchanged_for_vcf():
    items = extract_vcf(CARD.encode(), "utf-8")
    text = items[0]["text"]
    assert "Name: Jane Q. Doe" in text
    assert "Phone (cell): +1 555 0100" in text
    assert "Organization: Example Co, Sales; West\n" in text
    assert "1 Example St; Apt 2, Springfield" in text
    assert "Note: Line one\nLine two" in text


# --- parse_vcard ---


def test_vcard_structured():
    c = parse_vcard(CARD)
    assert c.version == "3.0"
    assert c.all("N")[0].parts == ("Doe", "Jane", "Q", "Dr.", "Jr.")
    assert c.all("FN")[0].value == "Jane Q. Doe"
    assert c.all("ORG")[0].parts == ("Example Co", "Sales; West")
    assert c.all("NOTE")[0].value == "Line one\\nLine two"
    email = c.all("EMAIL")[0]
    assert email.group == "item1" and email.params == {"TYPE": "INTERNET,work"}
    assert c.all("TEL")[0].params["TYPE"] == "cell"
    assert c.all("ADR")[0].parts == (
        "",
        "",
        "1 Example St; Apt 2",
        "Springfield",
        "ST",
        "00000",
        "Exampleland",
    )
    assert c.all("BDAY")[0].value == "1990-01-02"
    for dropped in ("URL", "PHOTO", "IMPP", "X-SOCIALPROFILE"):
        assert len(c.all(dropped)) == 1
    assert c.all("PHOTO")[0].params["ENCODING"] == "b"


def test_vcard_folded_lines_and_blank_lines():
    c = parse_vcard("BEGIN:VCARD\r\n\r\nFN:Jane\r\n  Doe\r\nEND:VCARD\r\n")
    assert c.all("FN")[0].value == "Jane Doe"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "FN:x",
        "BEGIN:VCARD\nFN:x\n",
        "BEGIN:VCARD\nFN:x\nEND:VCARD\nBEGIN:VCARD\nFN:y\nEND:VCARD\n",
        "BEGIN:VCARD\nBEGIN:VCARD\nFN:x\nEND:VCARD\nEND:VCARD\n",
        "BEGIN:VCARD\nno colon\nEND:VCARD\n",
        "BEGIN:VCALENDAR\nEND:VCALENDAR\n",
    ],
)
def test_vcard_rejects_malformed(text):
    with pytest.raises(VParseError):
        parse_vcard(text)


def test_vcard_limits(monkeypatch):
    monkeypatch.setattr(vcal, "MAX_PROPS", 3)
    body = "".join(f"NOTE:{i}\n" for i in range(4))
    with pytest.raises(VParseError, match="properties"):
        parse_vcard(f"BEGIN:VCARD\n{body}END:VCARD\n")
    monkeypatch.setattr(vcal, "MAX_LINES", 3)
    with pytest.raises(VParseError, match="lines"):
        parse_vcard("BEGIN:VCARD\nA:1\nB:2\nEND:VCARD\n")
    monkeypatch.setattr(vcal, "MAX_INPUT_CHARS", 10)
    with pytest.raises(VParseError, match="large"):
        parse_vcard(CARD)


# --- parse_icalendar ---


def test_icalendar_events():
    cal = parse_icalendar(ICS)
    assert cal.method == "REQUEST"
    assert cal.has_timezone is True
    assert len(cal.events) == 2  # the VTODO is ignored
    ev = cal.events[0]
    assert ev.all("DTSTART")[0].params == {"TZID": "Europe/Paris"}
    assert ev.all("DURATION")[0].value == "PT1H"
    assert ev.all("RRULE")[0].value == "FREQ=WEEKLY;BYDAY=TU"
    assert ev.all("EXDATE")[0].params["TZID"] == "Europe/Paris"
    assert ev.all("RDATE")[0].params["VALUE"] == "DATE"
    assert ev.all("STATUS")[0].value == "CONFIRMED"
    org = ev.all("ORGANIZER")[0]
    assert org.params["CN"] == "Boss: The" and org.value == "mailto:boss@example.org"
    att = ev.all("ATTENDEE")[0]
    assert att.params["PARTSTAT"] == "ACCEPTED" and att.params["RSVP"] == "TRUE"
    assert not ev.all("TRIGGER") and not ev.all("ACTION")  # alarm skipped
    assert not ev.all("TZOFFSETTO")
    assert cal.events[1].all("RECURRENCE-ID")[0].value == "20260317T090000"
    assert not any(p.name == "SUMMARY" and p.value == "Not an event" for e in cal.events for p in e.props)


def test_icalendar_without_timezone_or_events():
    cal = parse_icalendar("BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n")
    assert cal.has_timezone is False and cal.events == () and cal.method is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "BEGIN:VEVENT\nEND:VEVENT\n",
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nEND:VCALENDAR\n",
        "BEGIN:VCALENDAR\nEND:VEVENT\n",
        "BEGIN:VCALENDAR\nEND:VCALENDAR\nBEGIN:VCALENDAR\nEND:VCALENDAR\n",
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:x\nEND:VEVENT\n",
        "BEGIN:VCALENDAR\nsummary without colon\nEND:VCALENDAR\n",
    ],
)
def test_icalendar_rejects_malformed(text):
    with pytest.raises(VParseError):
        parse_icalendar(text)


def test_icalendar_limits(monkeypatch):
    ev = "BEGIN:VEVENT\nSUMMARY:x\nEND:VEVENT\n"
    monkeypatch.setattr(vcal, "MAX_EVENTS", 2)
    with pytest.raises(VParseError, match="events"):
        parse_icalendar("BEGIN:VCALENDAR\n" + ev * 3 + "END:VCALENDAR\n")
    monkeypatch.setattr(vcal, "MAX_EVENTS", 5)
    monkeypatch.setattr(vcal, "MAX_PROPS", 3)
    with pytest.raises(VParseError, match="properties"):
        parse_icalendar("BEGIN:VCALENDAR\n" + ev * 2 + "BEGIN:VEVENT\nA:1\nB:2\nEND:VEVENT\nEND:VCALENDAR\n")
    monkeypatch.setattr(vcal, "MAX_DEPTH", 2)
    with pytest.raises(VParseError, match="nested"):
        parse_icalendar(
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nBEGIN:VALARM\nEND:VALARM\nEND:VEVENT\nEND:VCALENDAR\n"
        )
    monkeypatch.setattr(vcal, "MAX_INPUT_CHARS", 10)
    with pytest.raises(VParseError, match="large"):
        parse_icalendar(ICS)


def test_module_imports_only_stdlib():
    src = open(vcal.__file__, encoding="utf-8").read()
    imports = [ln for ln in src.splitlines() if ln.startswith(("import ", "from "))]
    assert imports == ["from __future__ import annotations", "import re", "from dataclasses import dataclass"]
