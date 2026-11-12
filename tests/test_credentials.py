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


@pytest.fixture
def tty_cli(monkeypatch):
    """set-password on a terminal, with getpass and the clipboard under test control."""
    monkeypatch.setattr(cli, "load_config", lambda: make_cfg_for_cli())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    state = {"clip": "", "typed": "typed-pw", "answers": [], "cleared": False, "getpass_calls": 0}

    def fake_getpass(_prompt):
        state["getpass_calls"] += 1
        return state["typed"]

    monkeypatch.setattr(cli, "_clipboard_text", lambda: state["clip"])
    monkeypatch.setattr(cli, "_clear_clipboard", lambda: state.__setitem__("cleared", True) or True)
    monkeypatch.setattr(cli.getpass, "getpass", fake_getpass)
    monkeypatch.setattr("builtins.input", lambda _prompt: state["answers"].pop(0))
    return state


def test_set_password_empty_clipboard_goes_to_prompt(tty_cli, capsys):
    assert cli.main(["set-password", "icloud", "--store", "file"]) == 0
    assert credentials.get_password("icloud") == "typed-pw"
    assert tty_cli["getpass_calls"] == 1 and not tty_cli["cleared"]
    assert "clipboard" not in capsys.readouterr().out


def test_set_password_uses_clipboard_when_chosen(tty_cli, capsys):
    tty_cli["clip"] = "abcd-efgh-ijkl-mnop"
    tty_cli["answers"] = ["x", "1"]  # a wrong answer is asked again
    assert cli.main(["set-password", "icloud", "--store", "file"]) == 0
    assert credentials.get_password("icloud") == "abcd-efgh-ijkl-mnop"
    assert tty_cli["getpass_calls"] == 0 and tty_cli["cleared"]
    out = capsys.readouterr().out
    assert "holds 19 characters" in out
    assert "abcd-efgh-ijkl-mnop" not in out


def test_set_password_clipboard_declined_goes_to_prompt(tty_cli):
    tty_cli["clip"] = "something else I copied"
    tty_cli["answers"] = ["2"]
    assert cli.main(["set-password", "icloud", "--store", "file"]) == 0
    assert credentials.get_password("icloud") == "typed-pw"
    assert tty_cli["getpass_calls"] == 1 and not tty_cli["cleared"]


def make_cfg_for_cli():
    from pathlib import Path

    return make_cfg(Path("/nonexistent"))
