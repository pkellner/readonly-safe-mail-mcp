"""The `file` credential backend and the permission checks in `check`."""

import os
import sys

import pytest
from test_sync_query import make_cfg

from readonly_safe_mail_mcp import cli, credentials

posix = pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")


@pytest.fixture(autouse=True)
def cfg_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RSMM_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("RSMM_PASSWORD_ICLOUD", raising=False)
    return tmp_path / "cfg"


@posix
def test_file_backend_roundtrip_and_modes(cfg_dir):
    where = credentials.set_password("icloud", "s3cret", backend="file")
    f = cfg_dir / "credentials" / "icloud"
    assert str(f) in where
    assert os.stat(f).st_mode & 0o777 == 0o600 and os.stat(f.parent).st_mode & 0o777 == 0o700
    assert credentials.get_password("icloud") == "s3cret"
    os.chmod(f, 0o644)
    with pytest.raises(credentials.CredentialError, match="readable by other users"):
        credentials.get_password("icloud")


@posix
def test_env_var_wins_and_delete_removes_file(cfg_dir, monkeypatch):
    credentials.set_password("icloud", "fromfile", backend="file")
    monkeypatch.setenv("RSMM_PASSWORD_ICLOUD", "fromenv")
    assert credentials.get_password("icloud") == "fromenv"
    monkeypatch.setattr("keyring.delete_password", lambda *a: None)
    assert str(cfg_dir / "credentials" / "icloud") in credentials.delete_password("icloud")[0]
    assert not (cfg_dir / "credentials" / "icloud").exists()


@posix
def test_permission_warnings(tmp_path, cfg_dir):
    cfg = make_cfg(tmp_path)
    cfg.config_dir.mkdir(parents=True)
    (cfg.config_dir / "accounts.yaml").write_text("accounts: {}")
    os.chmod(cfg.config_dir / "accounts.yaml", 0o666)
    cfg.data_dir.mkdir(mode=0o755)
    os.chmod(cfg.data_dir, 0o755)
    w = "\n".join(cli.permission_warnings(cfg))
    assert "accounts.yaml can be changed by other users" in w
    assert f"{cfg.data_dir} can be read by other users" in w
    os.chmod(cfg.config_dir / "accounts.yaml", 0o600)
    os.chmod(cfg.data_dir, 0o700)
    assert not [x for x in cli.permission_warnings(cfg) if str(tmp_path) in x]


def test_set_password_refuses_without_tty(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: make_cfg_for_cli())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli.main(["set-password", "icloud", "--store", "file"]) == 2


def make_cfg_for_cli():
    from pathlib import Path

    return make_cfg(Path("/nonexistent"))
