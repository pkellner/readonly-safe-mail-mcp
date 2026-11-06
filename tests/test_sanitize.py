"""The sanitizer pipeline, including adversarial cases. Every message goes through
the real BODYSTRUCTURE parser via fakeimap.fetched()."""

from email.message import EmailMessage

import pytest
from fakeimap import fetched

from readonly_safe_mail_mcp.config import load_default_rules, merge_rules
from readonly_safe_mail_mcp.sanitize import (
    WITHHELD_SECURITY,
    WITHHELD_STRUCTURE,
    WITHHELD_TOO_LARGE,
    WITHHELD_UNDECODABLE,
    redact,
    sanitize_message,
)

RULES = merge_rules(load_default_rules(), [])


def make(
    subject="Hello", body="Hi", sender="Friend <friend@example.com>", html=None, cte=None
) -> EmailMessage:
    m = EmailMessage()
    m["From"] = sender
    m["To"] = "me@example.com"
    m["Subject"] = subject
    m["Date"] = "Fri, 06 Nov 2026 10:00:00 -0800"
    if body is not None:
        m.set_content(body, cte=cte) if cte else m.set_content(body)
        if html is not None:
            m.add_alternative(html, subtype="html")
    else:
        m.set_content(html, subtype="html")
    return m


def run(m, **kw):
    return sanitize_message(fetched(m, **kw), RULES)


def withheld(out) -> bool:
    return out.withheld and out.body == WITHHELD_SECURITY


# --- classification ---------------------------------------------------------


def test_reset_email_withheld_and_link_never_returned():
    out = run(make("Reset your password", "Click https://evil.example/r?t=abc"))
    assert withheld(out)
    assert "evil.example" not in repr(out)


def test_blocked_sender_withheld_even_with_innocent_text():
    assert withheld(run(make("Your receipt", "Thanks!", sender="Apple <no_reply@id.apple.com>")))


def test_withhold_reason_never_echoes_match():
    out = run(make("Hello", "your verification code is 482913"))
    assert withheld(out) and "verification" not in out.body and "482913" not in repr(out)


def test_word_boundary():
    assert not run(make("Dinner", "Chicken potpie tonight?")).withheld


@pytest.mark.parametrize(
    "body",
    [
        "Your p​assword was changed",  # zero-width space
        "Your pass­word was changed",  # soft hyphen
        "Your ｐａｓｓｗｏｒｄ was changed",  # fullwidth
        "Your p̲assword was changed",  # combining mark
        "Your one‐time code",  # unicode hyphen
        "Your PASSWORD was changed",
        "Your pass\x01word was changed",  # control char
    ],
)
def test_obfuscated_keywords_still_withhold(body):
    assert withheld(run(make("Hi", body)))


def test_entity_encoded_zero_width_in_html():
    assert withheld(run(make("Hi", body=None, html="<p>Your pass&#x200B;word was reset</p>")))


def test_keyword_split_across_html_tags():
    assert withheld(run(make("Hi", body=None, html="<p>pass<b></b><span>word</span> reset</p>")))


def test_qp_soft_line_break_inside_keyword():
    m = make("Hi", "Your pass=\nword was changed")
    m.replace_header("Content-Transfer-Encoding", "quoted-printable")
    assert withheld(run(m))


def test_keyword_only_in_html_alternative():
    assert withheld(run(make("Hi", "Nothing to see here", html="<p>Reset your password</p>")))


def test_keyword_only_in_hidden_html():
    assert withheld(run(make("Hi", body=None, html='<p>Hello</p><div style="display:none">password</div>')))


def test_keyword_only_in_quoted_reply():
    body = "Sounds good!\n\nOn Tue, Nov 4, 2026 at 3:12 PM Bank <b@x.com> wrote:\n> your verification code is 1234"
    assert withheld(run(make("Re: hi", body)))


def test_keyword_past_truncation_point():
    assert withheld(run(make("Hi", "filler " * 5000 + "password reset")))


@pytest.mark.xfail(
    strict=True, reason="known gap: cross-script homoglyphs (Cyrillic 'р', 'а') are not folded"
)
def test_homoglyph_keyword():
    assert withheld(run(make("Hi", "Your раssword was changed")))


# --- fail closed ------------------------------------------------------------


def test_invalid_base64_withholds_never_raw():
    m = make("Hi", "placeholder")
    m.set_payload("!!!@@@ not base64 ###")
    m.replace_header("Content-Transfer-Encoding", "base64")
    out = run(m)
    assert out.body == WITHHELD_UNDECODABLE and "@@@" not in repr(out)


def test_undecodable_charset_withholds():
    raw = (
        b"From: a@example.com\r\nSubject: Hi\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\n"
        b"\xff\xfe\xfa invalid utf-8\r\n"
    )
    out = run(raw)
    assert out.body == WITHHELD_UNDECODABLE and "invalid" not in repr(out)


def test_unparseable_structure_withholds():
    assert run(make(), structure='("text" garbage').body == WITHHELD_STRUCTURE


def test_oversized_text_part_withholds():
    assert run(make("Hi", "x" * 5000), max_part_bytes=1000).body == WITHHELD_TOO_LARGE


# --- display: hidden text, quotes -------------------------------------------


def test_hidden_html_never_displayed():
    html = (
        '<div style="display:none;max-height:0">preheader secret</div>'
        '<span style="font-size:0px">tiny</span><span style="font-size:0.9em">small but visible</span>'
        "<div hidden>attr hidden</div><p>Visible text</p><script>alert(1)</script>"
    )
    body = run(make("Hi", body=None, html=html)).body
    assert "Visible text" in body and "small but visible" in body
    for gone in ("preheader", "tiny", "attr hidden", "alert"):
        assert gone not in body


def test_html_hrefs_never_surface():
    out = run(make("Menu", body=None, html='<p>See <a href="https://tracker.example/x">our menu</a></p>'))
    assert "tracker.example" not in repr(out) and "our menu" in out.body


def test_gmail_html_quote_removed():
    html = '<div>New reply</div><div class="gmail_quote">On Tue X wrote:<blockquote class="gmail_quote">old</blockquote></div>'
    out = run(make("Re", body=None, html=html))
    assert out.body == "New reply" and "quoted replies removed" in out.notes


def test_outlook_plain_tail_removed():
    body = "My answer.\n\nFrom: Jane <j@x.com>\nSent: Tuesday\nTo: Me\nSubject: Q\n\nOriginal question"
    assert run(make("RE: Q", body)).body == "My answer."


def test_inline_replies_kept():
    body = "On Tue, Nov 4 Jane <j@x.com> wrote:\n> question one?\nAnswer one.\n> question two?\nAnswer two."
    assert run(make("Re", body)).body == "Answer one.\nAnswer two."


def test_only_quoted_body_falls_back_to_quote():
    assert "the original" in run(make("Fwd", "> the original text")).body


def test_forwarded_content_kept():
    body = "FYI\n\n---------- Forwarded message ---------\nFrom: A <a@x.com>\nDate: Mon\nSubject: S\nTo: B\n\nThe content"
    assert "The content" in run(make("Fwd: S", body)).body


# --- redaction --------------------------------------------------------------


@pytest.mark.parametrize(
    "text,gone",
    [
        ("Menu at https://cafe.example/menu", "cafe.example"),
        ("go to www.cafe.example now", "cafe.example"),
        ("open hxxps://evil.example/r?t=1", "evil.example"),
        ("visit evil.example/reset?t=abc", "evil.example"),
        ("visit evil.com today", "evil.com"),
        ("at 10.0.0.5/reset now", "10.0.0.5"),
        ("token a8f3k2m9q1 here", "a8f3k2m9q1"),
        ("hash d41d8cd98f00b204e9800998ecf8427e", "d41d8cd9"),
        ("code AB12CD34", "AB12CD34"),
        ("code 482913", "482913"),
        ("code 482 913", "482 913"),
        ("code 4 8 2 9 1 3", "4 8 2 9"),
        ("code 4829-1300", "4829-1300"),
    ],
)
def test_redacted(text, gone):
    out, _, _ = redact(text)
    assert gone not in out


@pytest.mark.parametrize(
    "text",
    [
        "The printing will cost $2,500 total",
        "The printing will cost $2500 total",
        "about 2500 dollars for postage",
        "a 40% response rate",
        "the 2025 appeal letter",
        "on 2026-11-06 we mail it",
        "it is $1,299.50 each",
        "see report.pdf and readme.md",
        "write to jane@example.com",
        "covid19 precautions",
        "e.g. the letter",
    ],
)
def test_not_redacted(text):
    out, _, _ = redact(text)
    assert out == text


def test_codes_in_subject_display_name_and_filename():
    m = make("Your code: 482913", "x", sender="Acme 777123 <x@example.com>")
    m.add_attachment(b"x", maintype="application", subtype="pdf", filename="statement_482913.pdf")
    out = run(m)
    assert "482913" not in out.headers.subject
    assert "777123" not in out.headers.from_
    assert "482913" not in out.attachments[0].filename
    assert out.attachments[0].content_type == "application/pdf"


def test_body_truncated_at_cap():
    small = merge_rules(load_default_rules(), [("t", {"max_body_chars": 50})])
    out = sanitize_message(fetched(make("Long", "word " * 100)), small)
    assert out.body.endswith("[truncated]") and "body truncated" in out.notes


def test_attachment_listed_but_body_not_affected():
    m = make("Invoice", "Please see attached")
    m.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="invoice.pdf")
    out = run(m)
    assert out.body == "Please see attached"
    assert [(a.part_id, a.filename) for a in out.attachments] == [("2", "invoice.pdf")]


# --- found in the first real-mailbox sync (2026-11-06) ----------------------


@pytest.mark.parametrize(
    "text",
    [
        "Share: https%3A%2F%2Fmailchi.mp%2Fexample%2Fnew-post",
        "mailto:?body=Check%20out%20http%3A%2F%2Fevil.example%2Freset%3Ft%3Dabc",
        "Sign up%3A%20https%3A%2F%2Fsignup.example%2Fx",
        "redirect=https%253A%252F%252Fevil.example%252Freset",  # double-encoded
        "next=%2F%2Fevil.example%2Fpath",
    ],
)
def test_percent_encoded_urls_removed(text):
    out, links, _ = redact(text)
    assert links >= 1
    for frag in ("mailchi", "evil", "signup.example", "%2F%2F", "%252F"):
        assert frag not in out


def test_html_document_in_text_plain_is_converted():
    html = (
        '<!DOCTYPE html><html><head><meta content="text/html; charset=utf-8" http-equiv="Content-Type" />'
        '</head><body><p>Board meeting moved to Tuesday.</p><a href="https://evil.example/x">here</a></body></html>'
    )
    out = run(make(body=html))
    assert "Board meeting moved to Tuesday." in out.body
    assert "<meta" not in out.body and "http-equiv" not in out.body and "evil" not in out.body


def test_plain_text_mentioning_tags_is_left_alone():
    out = run(make(body="Use the <b> tag sparingly, and <i> too."))
    assert "<b>" in out.body
