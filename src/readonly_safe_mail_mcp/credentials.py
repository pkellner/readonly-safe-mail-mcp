"""Per-account passwords from the OS keyring (macOS Keychain, Windows Credential
Manager, Linux Secret Service). An environment variable fallback exists for
environments without a keyring, with a warning because it puts the password in
plain text in the MCP client's config.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

SERVICE = "readonly-safe-mail-mcp"


class CredentialError(RuntimeError):
    pass


def env_var_name(account: str) -> str:
    return "RSMM_PASSWORD_" + account.upper().replace("-", "_")


def get_password(account: str) -> str:
    pw = os.environ.get(env_var_name(account))
    if pw:
        log.warning(
            "account %r: password taken from %s (plain text in client config)", account, env_var_name(account)
        )
        return pw
    import keyring  # lazy: tests and `print-rules` must not need a keyring backend

    try:
        pw = keyring.get_password(SERVICE, account)
    except Exception as e:  # keyring raises backend-specific errors
        raise CredentialError(f"account {account!r}: keyring unavailable: {e}") from e
    if not pw:
        raise CredentialError(
            f"No password stored for account {account!r}. Run: readonly-safe-mail-mcp set-password {account}"
        )
    return pw


def set_password(account: str, password: str) -> None:
    import keyring

    keyring.set_password(SERVICE, account, password)


def delete_password(account: str) -> None:
    import keyring

    keyring.delete_password(SERVICE, account)
