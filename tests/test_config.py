import logging

import pytest

from readonly_safe_mail_mcp.config import (
    ConfigError,
    Folders,
    load_config,
    load_default_rules,
    merge_rules,
    parse_duration,
)

DEFAULTS = load_default_rules()


def write(tmp_path, name, text):
    (tmp_path / name).write_text(text)
    return tmp_path


# --- rule layering ----------------------------------------------------------


def test_defaults_alone():
    r = merge_rules(DEFAULTS, [])
    assert "paypal.com" in r.blocked_sender_domains
    assert "password" in r.sensitive_keywords
    assert r.max_body_chars == DEFAULTS["max_body_chars"]


def test_user_layer_adds_but_cannot_remove():
    r = merge_rules(
        DEFAULTS,
        [("user", {"blocked_sender_domains": ["mybank.example"], "sensitive_keywords": ["wire transfer"]})],
    )
    assert "mybank.example" in r.blocked_sender_domains
    assert "paypal.com" in r.blocked_sender_domains  # still there
    assert "wire transfer" in r.sensitive_keywords
    assert r.sender_is_blocked("alerts@mail.mybank.example") == "mybank.example"


def test_disable_default_requires_explicit_key_and_warns(caplog):
    with caplog.at_level(logging.WARNING):
        r = merge_rules(DEFAULTS, [("user", {"disable_default_keywords": ["reset"]})])
    assert "reset" not in r.sensitive_keywords
    assert "DISABLED" in caplog.text


def test_disable_nonexistent_default_is_error():
    with pytest.raises(ConfigError):
        merge_rules(DEFAULTS, [("user", {"disable_default_sender_domains": ["nope.example"]})])


def test_disable_is_order_independent():
    layers = [("a", {"sensitive_keywords": ["reset"]}), ("b", {"disable_default_keywords": ["reset"]})]
    assert "reset" in merge_rules(DEFAULTS, layers).sensitive_keywords  # re-added explicitly
    assert "reset" in merge_rules(DEFAULTS, layers[::-1]).sensitive_keywords


def test_caps_can_be_lowered_not_raised():
    r = merge_rules(DEFAULTS, [("user", {"max_body_chars": 1000})])
    assert r.max_body_chars == 1000
    with pytest.raises(ConfigError):
        merge_rules(DEFAULTS, [("user", {"max_body_chars": DEFAULTS["max_body_chars"] + 1})])


def test_unknown_rule_key_is_error():
    with pytest.raises(ConfigError, match="unknown key"):
        merge_rules(DEFAULTS, [("user", {"blocked_senders": ["x.com"]})])  # typo must fail loudly


def test_keyword_word_boundary():
    r = merge_rules(DEFAULTS, [])
    assert r.keyword_hit("Chicken potpie tonight?") is None
    assert r.keyword_hit("Your OTP is here") == "otp"


# --- accounts ---------------------------------------------------------------


def test_load_config_with_presets_and_overrides(tmp_path):
    write(
        tmp_path,
        "accounts.yaml",
        """
accounts:
  icloud: {preset: icloud, username: me@icloud.com}
  work:
    host: imap.example.com
    port: 993
    username: me@example.com
    folders: {allow: [INBOX, Archive]}
    rules: {blocked_sender_domains: [sso.example.com]}
sync_if_older_than: 2m
attachments: never
""",
    )
    write(tmp_path, "rules.yaml", "sensitive_keywords: [wire transfer]\n")
    cfg = load_config(tmp_path, tmp_path / "data")
    assert cfg.sync_if_older_than == 120
    assert cfg.attachments == "never"
    ic, work = cfg.accounts["icloud"], cfg.accounts["work"]
    assert (ic.host, ic.port) == ("imap.mail.me.com", 993)
    assert ic.folders == Folders()
    assert work.folders.permits("Archive") and not work.folders.permits("INBOX2")
    assert not ic.folders.permits("Junk")
    assert "wire transfer" in ic.rules.sensitive_keywords
    assert "wire transfer" in work.rules.sensitive_keywords
    assert "sso.example.com" in work.rules.blocked_sender_domains
    assert "sso.example.com" not in ic.rules.blocked_sender_domains


def test_missing_accounts_file(tmp_path):
    with pytest.raises(ConfigError, match="No accounts file"):
        load_config(tmp_path, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        "accounts: {Bad Name: {preset: icloud, username: x@y}}",
        "accounts: {a: {preset: nope, username: x@y}}",
        "accounts: {a: {username: x@y}}",
        "accounts: {a: {preset: icloud}}",
        "accounts: {a: {preset: icloud, username: x@y, extra: 1}}",
        "accounts: {a: {preset: icloud, username: x@y}}\nattachments: yes-please",
        "accounts: {a: {preset: icloud, username: x@y}}\nunknown_top: 1",
        "accounts: {}",
    ],
)
def test_bad_account_configs(tmp_path, body):
    write(tmp_path, "accounts.yaml", body)
    with pytest.raises(ConfigError):
        load_config(tmp_path, tmp_path)


@pytest.mark.parametrize("value,expected", [("5m", 300), ("90s", 90), ("1h", 3600), (45, 45), ("30", 30)])
def test_parse_duration(value, expected):
    assert parse_duration(value) == expected


def test_parse_duration_rejects_garbage():
    with pytest.raises(ConfigError):
        parse_duration("soon")
