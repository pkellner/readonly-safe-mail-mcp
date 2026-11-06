"""TLS must be verified before any credential is sent."""

import ssl
from email.message import EmailMessage

import pytest
import trustme
from fakeimap import FakeIMAPServer

from readonly_safe_mail_mcp.config import Account, Folders, load_default_rules, merge_rules
from readonly_safe_mail_mcp.imap import MailSource, PolicyError

RULES = merge_rules(load_default_rules(), [])


def account(port: int) -> Account:
    return Account("t", "localhost", port, "me@example.com", Folders(), RULES)


def server_ctx(ca: trustme.CA, hostname: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ca.issue_cert(hostname).configure_cert(ctx)
    return ctx


def inbox():
    m = EmailMessage()
    m["Subject"] = "x"
    m.set_content("x")
    return {"INBOX": {1: m.as_bytes()}}


def test_trusted_certificate_connects():
    ca = trustme.CA()
    client = ssl.create_default_context()
    ca.configure_trust(client)
    with FakeIMAPServer(inbox(), ssl_context=server_ctx(ca, "localhost")) as server:
        with MailSource.open(account(server.port), "pw", ssl_context=client, timeout=5) as src:
            assert src.examine("INBOX").exists == 1
    assert server.logins == 1  # proves the negative tests below are not vacuous


def test_untrusted_certificate_blocks_login():
    ca = trustme.CA()  # never trusted by the default context
    with FakeIMAPServer(inbox(), ssl_context=server_ctx(ca, "localhost")) as server:
        with pytest.raises(ssl.SSLCertVerificationError):
            MailSource.open(account(server.port), "pw", timeout=5)
    assert server.logins == 0


def test_wrong_hostname_blocks_login():
    ca = trustme.CA()
    client = ssl.create_default_context()
    ca.configure_trust(client)
    with FakeIMAPServer(inbox(), ssl_context=server_ctx(ca, "imap.other.example")) as server:
        with pytest.raises(ssl.SSLCertVerificationError):
            MailSource.open(account(server.port), "pw", ssl_context=client, timeout=5)
    assert server.logins == 0


def test_default_context_uses_os_trust_store_and_verifies():
    from readonly_safe_mail_mcp.imap import default_tls_context

    ctx = default_tls_context()
    assert type(ctx).__module__.startswith("truststore")
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_unverified_context_refused():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with pytest.raises(PolicyError):
        MailSource.open(account(1), "pw", ssl_context=ctx)


# --- `check` command, end to end ----------------------------------------------


def test_check_command_reports_newest_sanitized_header(monkeypatch):
    from readonly_safe_mail_mcp import cli, imap

    ca = trustme.CA()

    def trusting():
        ctx = ssl.create_default_context()
        ca.configure_trust(ctx)
        return ctx

    m = EmailMessage()
    m["From"] = "Bank <alerts@bank.example>"
    m["Subject"] = "Your code is 482913"
    m["Date"] = "Fri, 06 Nov 2026 10:00:00 -0800"
    m.set_content("x")
    with FakeIMAPServer({"INBOX": {7: m.as_bytes()}}, ssl_context=server_ctx(ca, "localhost")) as server:
        monkeypatch.setattr(imap, "default_tls_context", trusting)
        ok, lines = cli._connection_check(account(server.port), "pw")
    assert ok, lines
    assert any("TLS verified" in ln for ln in lines)
    newest = next(ln for ln in lines if ln.startswith("newest:"))
    assert "Bank" in newest and "482913" not in newest
    assert not any("BODY.PEEK[1]" in c for c in server.commands)  # check never reads bodies


def test_check_command_untrusted_cert_says_password_not_sent():
    from readonly_safe_mail_mcp import cli

    with FakeIMAPServer(inbox(), ssl_context=server_ctx(trustme.CA(), "localhost")) as server:
        ok, lines = cli._connection_check(account(server.port), "pw")
    assert not ok and "password was not sent" in lines[-1]
    assert server.logins == 0
