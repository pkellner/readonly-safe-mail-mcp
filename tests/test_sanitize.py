from email.message import EmailMessage

import pytest

from readonly_safe_mail_mcp.config import load_default_rules, merge_rules
from readonly_safe_mail_mcp.sanitize import sanitize_headers, sanitize_message

RULES = merge_rules(load_default_rules(), [])


def make(subject, body, sender="friend@example.com", html=False):
    m = EmailMessage()
    m["From"] = sender
    m["To"] = "me@example.com"
    m["Subject"] = subject
    m["Date"] = "Fri, 06 Nov 2026 10:00:00 -0800"
    m.set_content(body, subtype="html" if html else "plain")
    return m


# --- withheld bodies --------------------------------------------------------


def test_password_reset_body_withheld():
    out = sanitize_message(make("Reset your password", "Click https://evil.example/r?t=abc"), RULES)
    assert "withheld" in out["body"]
    assert "evil.example" not in str(out)


def test_keyword_only_in_body_still_withholds():
    out = sanitize_message(make("Hello", "Your verification code is 482913"), RULES)
    assert "withheld" in out["body"]
    assert "482913" not in str(out)


def test_blocked_sender_withheld_even_with_innocent_text():
    out = sanitize_message(make("Your receipt", "Thanks!", sender="Apple <no_reply@id.apple.com>"), RULES)
    assert "withheld" in out["body"]
    assert "Thanks" not in out["body"]


def test_withhold_reason_is_generic():
    out = sanitize_message(make("Reset your password", "x"), RULES)
    assert "password" not in out["body"].lower().replace("account-security", "")


def test_keyword_word_boundary():
    out = sanitize_message(make("Dinner", "Chicken potpie tonight?"), RULES)
    assert "withheld" not in out["body"]


# --- links and codes in normal mail ----------------------------------------


def test_links_always_removed_from_normal_mail():
    out = sanitize_message(make("Lunch", "Menu at https://cafe.example/menu and www.cafe.example"), RULES)
    assert "cafe.example" not in out["body"]
    assert out["body"].count("[link removed]") == 2


def test_html_hrefs_never_surface():
    html = '<p>See <a href="https://tracker.example/x">our menu</a></p><script>alert(1)</script>'
    out = sanitize_message(make("Menu", html, html=True), RULES)
    assert "tracker.example" not in str(out)
    assert "alert" not in out["body"]
    assert "our menu" in out["body"]


@pytest.mark.parametrize(
    "subject",
    ["Your code: 482913", "Code 482 913 for login", "Use 4829-1300", "PIN 4821", "Token AB12CD34"],
)
def test_codes_masked_in_subject(subject):
    out = sanitize_headers(make(subject, "x"), RULES)
    for digit_run in ("482913", "482 913", "4829-1300", "4821", "AB12CD34"):
        assert digit_run not in out["subject"]
    assert "[code hidden]" in out["subject"]


def test_ordinary_subject_untouched():
    out = sanitize_headers(make("Dinner on Saturday?", "x"), RULES)
    assert out["subject"] == "Dinner on Saturday?"


def test_body_truncated_at_cap():
    small = merge_rules(load_default_rules(), [("t", {"max_body_chars": 50})])
    out = sanitize_message(make("Long", "word " * 100), small)
    assert out["body"].endswith("[truncated]")
    assert "body truncated" in out["filter_notes"]
