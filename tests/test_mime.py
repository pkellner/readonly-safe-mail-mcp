import base64

import pytest

from readonly_safe_mail_mcp.mime import (
    DecodeError,
    ResponseParseError,
    StructureError,
    decode_part,
    parse_bodystructure,
    parse_fetch,
    parse_list,
    parse_response,
)


def bs(text: str):
    return parse_bodystructure(parse_response([text.encode()])[0])


# --- response parsing -------------------------------------------------------


def test_fetch_with_literal_and_bracketed_key():
    data = [(b"1 (UID 7 RFC822.SIZE 99 BODY[HEADER.FIELDS (FROM SUBJECT)]<0> {12}", b"Subject: hi\r\n"), b")"]
    d = parse_fetch(data)[7]
    assert d["RFC822.SIZE"] == "99"
    assert d["BODY[HEADER.FIELDS (FROM SUBJECT)]<0>"] == b"Subject: hi\r\n"


def test_fetch_ignores_responses_without_uid():
    assert parse_fetch([b"3 (FLAGS (\\Seen))"]) == {}


def test_list_parsing():
    data = [
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\Junk \\HasNoChildren) "/" "Junk"',
        (b'() "/" {6}', b"Caf\xc3\xa9"),
    ]
    assert parse_list(data) == [
        (("\\HasNoChildren",), "INBOX"),
        (("\\Junk", "\\HasNoChildren"), "Junk"),
        ((), "Café"),
    ]


@pytest.mark.parametrize("bad", [b'(UID 1 "unterminated', b"(UID 1", b"UID 1)", b"(" * 100 + b")" * 100])
def test_malformed_responses_raise(bad):
    with pytest.raises(ResponseParseError):
        parse_response([bad])


# --- BODYSTRUCTURE ----------------------------------------------------------


def test_single_part():
    st = bs('("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 12 1)')
    (p,) = st.leaves
    assert (p.part_id, p.content_type, p.params["charset"], p.is_body_text) == (
        "1",
        "text/plain",
        "utf-8",
        True,
    )


def test_nested_mixed_alternative_with_attachment():
    st = bs(
        '((("text" "plain" NIL NIL NIL "7bit" 5 1)("text" "html" NIL NIL NIL "7bit" 9 1) "alternative")'
        '("application" "pdf" ("name" "a.pdf") NIL NIL "base64" 300 NIL ("attachment" ("filename" "a.pdf")) NIL NIL)'
        ' "mixed")'
    )
    assert [p.part_id for p in st.leaves] == ["1.1", "1.2", "2"]
    assert [p.part_id for p in st.body_text_parts()] == ["1.1", "1.2"]
    assert [p.filename for p in st.attachments()] == ["a.pdf"]
    assert [p.part_id for p in st.display_parts("plain")] == ["1.1"]
    assert [p.part_id for p in st.display_parts("html")] == ["1.2"]


def test_attached_message_is_not_descended():
    st = bs(
        '(("text" "plain" NIL NIL NIL "7bit" 5 1)'
        '("message" "rfc822" NIL NIL NIL "7bit" 50 NIL ("text" "plain" NIL NIL NIL "7bit" 5 1) 3 NIL NIL NIL NIL)'
        ' "mixed")'
    )
    assert [p.part_id for p in st.leaves] == ["1", "2"]
    assert st.part("2").filename == "attached-message.eml"
    assert not st.part("2").is_body_text


def test_inline_text_with_filename_is_an_attachment():
    st = bs('("text" "plain" NIL NIL NIL "7bit" 5 1 NIL ("inline" ("filename" "notes.txt")) NIL NIL)')
    assert st.body_text_parts() == []


def test_filename_as_literal_rfc2231_and_rfc2047():
    data = [
        (
            b'(("text" "plain" NIL NIL NIL "7bit" 5 1)("application" "pdf" NIL NIL NIL "base64" 9 NIL'
            b' ("attachment" ("filename" {10}',
            b"r\xc3\xa9sum\xc3\xa9.pdf"[:10],
        ),
        b')) NIL NIL) "mixed")',
    ]
    st = parse_bodystructure(parse_response(data)[0])
    assert st.part("2").filename.startswith("résum")
    st = bs(
        '("application" "pdf" NIL NIL NIL "base64" 9 NIL ("attachment" ("filename*" "utf-8\'\'na%C3%AFve.pdf")) NIL NIL)'
    )
    assert st.leaves[0].filename == "naïve.pdf"
    st = bs('("application" "pdf" ("name" "=?utf-8?B?w7xiZXIucGRm?=") NIL NIL "base64" 9 NIL NIL NIL NIL)')
    assert st.leaves[0].filename == "über.pdf"


def test_depth_and_part_limits():
    deep = '("text" "plain" NIL NIL NIL "7bit" 1 1)'
    for _ in range(25):
        deep = f'({deep} "mixed")'
    with pytest.raises(StructureError):
        bs(deep)
    wide = "(" + '("image" "png" NIL NIL NIL "base64" 1 NIL NIL NIL NIL)' * 201 + ' "mixed")'
    with pytest.raises(StructureError):
        bs(wide)


@pytest.mark.parametrize("bad", ['("text")', "NIL", '"just a string"', '(("text" "plain") "mixed")'])
def test_malformed_structure(bad):
    with pytest.raises(StructureError):
        bs(bad)


# --- decoding ---------------------------------------------------------------


def test_decode_base64_and_qp():
    assert decode_part(base64.b64encode("héllo".encode()), "base64", "utf-8") == "héllo"
    assert decode_part(b"pass=\r\nword", "quoted-printable", "us-ascii") == "password"


def test_decode_missing_padding_tolerated():
    assert decode_part(b"aGVsbG8", "base64", "utf-8") == "hello"


@pytest.mark.parametrize(
    "raw,enc,cs",
    [
        (b"!!!not base64!!!", "base64", "utf-8"),
        (b"\xff\xfe\xfa", "8bit", "utf-8"),
        (b"x", "x-uuencode", "utf-8"),
        (b"\xff\xfe\xfa", "8bit", "zlib_codec"),  # non-text codec must not be usable
    ],
)
def test_decode_failures_raise(raw, enc, cs):
    with pytest.raises(DecodeError):
        decode_part(raw, enc, cs)


def test_mislabeled_ascii_falls_back_to_utf8():
    assert decode_part("café".encode(), "8bit", "us-ascii") == "café"
    assert decode_part("café".encode(), "8bit", "x-unknown-charset") == "café"
