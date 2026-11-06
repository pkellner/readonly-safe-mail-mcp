"""Per-account passwords.

Lookup order:
  1. RSMM_PASSWORD_<ACCOUNT> environment variable (warns: plain text in client config)
  2. <config dir>/credentials/<account>  (the `file` backend, for dedicated-user installs;
     see docs/THREAT_MODEL.md. Refused unless owned by the running user with mode 0600.)
  3. the OS keyring (macOS Keychain, Windows Credential Manager, Linux Secret Service)
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

SERVICE = "readonly-safe-mail-mcp"
BACKENDS = ("keyring", "file")


class CredentialError(RuntimeError):
    pass


def env_var_name(account: str) -> str:
    return "RSMM_PASSWORD_" + account.upper().replace("-", "_")


def credentials_dir() -> Path:
    return paths.config_dir() / "credentials"


def credential_file(account: str) -> Path:
    return credentials_dir() / account


def _read_file(path: Path, account: str) -> str:
    st = path.stat()
    if os.name == "posix":
        if st.st_uid != os.getuid():
            raise CredentialError(f"account {account!r}: {path} is owned by another user; refusing to use it")
        if st.st_mode & 0o077:
            raise CredentialError(
                f"account {account!r}: {path} is readable by other users "
                f"(mode {stat.filemode(st.st_mode)}); run: chmod 600 {path}"
            )
    pw = path.read_text(encoding="utf-8").rstrip("\r\n")
    if not pw:
        raise CredentialError(f"account {account!r}: {path} is empty")
    return pw


def get_password(account: str) -> str:
    pw = os.environ.get(env_var_name(account))
    if pw:
        log.warning(
            "account %r: password taken from %s (plain text in client config)", account, env_var_name(account)
        )
        return pw
    f = credential_file(account)
    if f.exists():
        return _read_file(f, account)
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


def set_password(account: str, password: str, backend: str = "keyring") -> str:
    """Store a password. Returns a description of where it went."""
    if backend == "file":
        d = credentials_dir()
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        f = credential_file(account)
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(password)
        os.chmod(f, 0o600)
        return f"file {f} (mode 600)"
    import keyring

    keyring.set_password(SERVICE, account, password)
    return f"the OS keyring (service {SERVICE!r})"


def delete_password(account: str) -> list[str]:
    """Remove every local copy. Returns where copies were removed from."""
    removed = []
    f = credential_file(account)
    if f.exists():
        f.unlink()
        removed.append(str(f))
    import keyring
    import keyring.errors

    try:
        keyring.delete_password(SERVICE, account)
        removed.append("OS keyring")
    except keyring.errors.PasswordDeleteError:
        pass
    except Exception as e:
        log.warning("keyring unavailable: %s", e)
    return removed
