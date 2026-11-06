"""Configuration: accounts, folders, and layered redaction rules.

Layering is fail-closed (PLAN.md section 5):
  packaged defaults  ->  <config dir>/rules.yaml  ->  per-account `rules:` block
List fields are unioned, so user layers can only ADD protection. Removing a
default requires an explicit `disable_default_*` entry and logs a warning.
Size caps may be lowered but never raised above the packaged default.
Unknown keys anywhere are a hard error (a typo must not silently disable a rule).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from email.utils import parseaddr
from importlib import resources
from pathlib import Path

import yaml

from . import paths
from .textnorm import match_form

log = logging.getLogger(__name__)


class ConfigError(ValueError):
    """Raised for any malformed or unsafe configuration."""


# --- presets ----------------------------------------------------------------

PRESETS: dict[str, tuple[str, int]] = {
    "icloud": ("imap.mail.me.com", 993),
    "gmail": ("imap.gmail.com", 993),
    "fastmail": ("imap.fastmail.com", 993),
    "yahoo": ("imap.mail.yahoo.com", 993),
}

DEFAULT_FOLDER_DENY = ("Junk", "Spam", "Trash", "Deleted Messages", "Deleted Items", "Drafts")

ACCOUNT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

RULE_KEYS = frozenset(
    {
        "blocked_sender_domains",
        "sensitive_keywords",
        "max_body_chars",
        "max_message_bytes",
        "disable_default_sender_domains",
        "disable_default_keywords",
    }
)
TOP_KEYS = frozenset(
    {"accounts", "folders", "sync_if_older_than", "attachments", "trusted_elicitation_clients"}
)
ACCOUNT_KEYS = frozenset({"preset", "host", "port", "username", "folders", "rules"})
FOLDER_KEYS = frozenset({"allow", "deny"})
ATTACHMENT_MODES = ("ask", "never", "ask-unsandboxed")


# --- helpers ----------------------------------------------------------------


def _load_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def _check_keys(data: dict, allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {unknown}; allowed: {sorted(allowed)}")


def _str_list(value, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where}: must be a list of strings")
    return value


def _norm_domain(d: str) -> str:
    return d.lower().strip().lstrip("@.")


def _norm_keyword(k: str) -> str:
    return " ".join(k.lower().split())


_DURATION_RE = re.compile(r"^(\d+)\s*([smh]?)$")


def parse_duration(value, where: str = "sync_if_older_than") -> int:
    """'5m' -> 300, '90s' -> 90, '1h' -> 3600, 300 -> 300. Returns seconds."""
    if isinstance(value, bool):
        raise ConfigError(f"{where}: must be a duration like '5m'")
    if isinstance(value, int):
        if value < 0:
            raise ConfigError(f"{where}: must not be negative")
        return value
    m = _DURATION_RE.match(str(value).strip())
    if not m:
        raise ConfigError(f"{where}: must be a duration like '5m', '90s' or '1h'")
    n, unit = int(m.group(1)), m.group(2)
    return n * {"": 1, "s": 1, "m": 60, "h": 3600}[unit]


# --- rules ------------------------------------------------------------------


def load_default_rules() -> dict:
    text = (resources.files(__package__) / "default_rules.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    _check_keys(data, RULE_KEYS, "default_rules.yaml")
    return data


@dataclass
class Rules:
    blocked_sender_domains: tuple[str, ...]
    sensitive_keywords: tuple[str, ...]
    max_body_chars: int
    max_message_bytes: int
    _kw_re: re.Pattern | None = field(init=False, repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if self.sensitive_keywords:
            # Keywords and text are both reduced to match_form (NFKD, marks and format chars
            # removed, case-folded), so "p\u200bassword" and "ｐａｓｓｗｏｒｄ" still match.
            forms = sorted({match_form(k) for k in self.sensitive_keywords} - {""}, key=len, reverse=True)
            alternation = "|".join(re.escape(k) for k in forms)
            # Word-boundary match so "otp" doesn't hit "potpie".
            self._kw_re = re.compile(rf"(?<!\w)({alternation})(?!\w)")

    def sender_is_blocked(self, from_header: str) -> str | None:
        addr = parseaddr(from_header or "")[1].lower()
        domain = addr.rsplit("@", 1)[-1] if "@" in addr else ""
        for d in self.blocked_sender_domains:
            if domain == d or domain.endswith("." + d):
                return d
        return None

    def keyword_hit(self, *texts: str) -> str | None:
        if not self._kw_re:
            return None
        for t in texts:
            m = self._kw_re.search(match_form(t or ""))
            if m:
                return m.group(1)
        return None

    def as_dict(self) -> dict:
        return {
            "blocked_sender_domains": list(self.blocked_sender_domains),
            "sensitive_keywords": list(self.sensitive_keywords),
            "max_body_chars": self.max_body_chars,
            "max_message_bytes": self.max_message_bytes,
        }


def merge_rules(default: dict, layers: list[tuple[str, dict]]) -> Rules:
    """Combine packaged defaults with user layers. Order-independent:
    result = (defaults - explicit disables) | all additions."""
    default_domains = {_norm_domain(d) for d in _str_list(default.get("blocked_sender_domains"), "defaults")}
    default_kws = {_norm_keyword(k) for k in _str_list(default.get("sensitive_keywords"), "defaults")}
    max_body = int(default["max_body_chars"])
    max_bytes = int(default["max_message_bytes"])

    add_domains: set[str] = set()
    add_kws: set[str] = set()
    for where, layer in layers:
        if not isinstance(layer, dict):
            raise ConfigError(f"{where}: rules must be a mapping")
        _check_keys(layer, RULE_KEYS, where)

        for d in _str_list(
            layer.get("disable_default_sender_domains"), f"{where}.disable_default_sender_domains"
        ):
            nd = _norm_domain(d)
            if nd not in default_domains:
                raise ConfigError(f"{where}: {d!r} is not a default sender domain, nothing to disable")
            log.warning("%s: default blocked sender domain %r DISABLED by user config", where, nd)
            default_domains.discard(nd)
        for k in _str_list(layer.get("disable_default_keywords"), f"{where}.disable_default_keywords"):
            nk = _norm_keyword(k)
            if nk not in default_kws:
                raise ConfigError(f"{where}: {k!r} is not a default keyword, nothing to disable")
            log.warning("%s: default sensitive keyword %r DISABLED by user config", where, nk)
            default_kws.discard(nk)

        add_domains |= {_norm_domain(d) for d in _str_list(layer.get("blocked_sender_domains"), where)}
        add_kws |= {_norm_keyword(k) for k in _str_list(layer.get("sensitive_keywords"), where) if k.strip()}

        for key in ("max_body_chars", "max_message_bytes"):
            if key not in layer:
                continue
            v = layer[key]
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise ConfigError(f"{where}: {key} must be a positive integer")
            if v > int(default[key]):
                raise ConfigError(
                    f"{where}: {key}={v} exceeds packaged default {default[key]}; caps can only be lowered"
                )
            if key == "max_body_chars":
                max_body = min(max_body, v)
            else:
                max_bytes = min(max_bytes, v)

    domains = {d for d in default_domains | add_domains if d}
    kws = {k for k in default_kws | add_kws if k}
    return Rules(tuple(sorted(domains)), tuple(sorted(kws)), max_body, max_bytes)


# --- accounts ---------------------------------------------------------------


@dataclass(frozen=True)
class Folders:
    allow: tuple[str, ...] = ("*",)
    deny: tuple[str, ...] = DEFAULT_FOLDER_DENY

    def permits(self, folder: str) -> bool:
        if folder in self.deny:
            return False
        return "*" in self.allow or folder in self.allow


def _parse_folders(data, where: str, base: Folders) -> Folders:
    if data is None:
        return base
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: folders must be a mapping with allow/deny")
    _check_keys(data, FOLDER_KEYS, where)
    allow = tuple(_str_list(data.get("allow"), f"{where}.allow")) or base.allow
    deny = tuple(_str_list(data.get("deny"), f"{where}.deny")) if "deny" in data else base.deny
    return Folders(allow=allow, deny=deny)


@dataclass(frozen=True)
class Account:
    name: str
    host: str
    port: int
    username: str
    folders: Folders
    rules: Rules


@dataclass(frozen=True)
class Config:
    accounts: dict[str, Account]
    sync_if_older_than: int  # seconds
    attachments: str
    trusted_elicitation_clients: tuple[str, ...]
    config_dir: Path
    data_dir: Path


def _parse_account(
    name: str, data, user_rule_layers: list[tuple[str, dict]], base_folders: Folders
) -> Account:
    where = f"accounts.{name}"
    if not ACCOUNT_NAME_RE.match(name):
        raise ConfigError(f"{where}: account names must match {ACCOUNT_NAME_RE.pattern}")
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: must be a mapping")
    _check_keys(data, ACCOUNT_KEYS, where)

    preset = data.get("preset")
    if preset is not None:
        if preset not in PRESETS:
            raise ConfigError(f"{where}: unknown preset {preset!r}; known: {sorted(PRESETS)}")
        host, port = PRESETS[preset]
    else:
        host, port = data.get("host"), data.get("port", 993)
    if "host" in data:
        host = data["host"]
    if "port" in data:
        port = data["port"]
    if not isinstance(host, str) or not host.strip():
        raise ConfigError(f"{where}: needs `preset` or `host`")
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise ConfigError(f"{where}: port must be an integer 1-65535")

    username = data.get("username")
    if not isinstance(username, str) or not username.strip():
        raise ConfigError(f"{where}: `username` is required")

    layers = list(user_rule_layers)
    if "rules" in data:
        layers.append((f"{where}.rules", data["rules"]))
    rules = merge_rules(load_default_rules(), layers)
    folders = _parse_folders(data.get("folders"), f"{where}.folders", base_folders)
    return Account(
        name=name, host=host.strip(), port=port, username=username.strip(), folders=folders, rules=rules
    )


def load_config(config_dir: Path | None = None, data_dir: Path | None = None) -> Config:
    cdir = config_dir or paths.config_dir()
    ddir = data_dir or paths.data_dir()
    accounts_path = cdir / "accounts.yaml"
    if not accounts_path.exists():
        raise ConfigError(
            f"No accounts file at {accounts_path}. Create it (see README) with at least one account."
        )
    top = _load_yaml(accounts_path)
    _check_keys(top, TOP_KEYS, str(accounts_path))

    user_layers: list[tuple[str, dict]] = []
    rules_path = cdir / "rules.yaml"
    if rules_path.exists():
        user_layers.append((str(rules_path), _load_yaml(rules_path)))

    base_folders = _parse_folders(top.get("folders"), f"{accounts_path}:folders", Folders())

    raw_accounts = top.get("accounts")
    if not isinstance(raw_accounts, dict) or not raw_accounts:
        raise ConfigError(f"{accounts_path}: `accounts` must be a non-empty mapping")
    accounts = {
        name: _parse_account(name, data, user_layers, base_folders) for name, data in raw_accounts.items()
    }

    attachments = top.get("attachments", "ask")
    if attachments not in ATTACHMENT_MODES:
        raise ConfigError(f"{accounts_path}: attachments must be one of {ATTACHMENT_MODES}")
    if attachments == "ask-unsandboxed":
        log.warning("attachments: ask-unsandboxed — attachment parsing will run WITHOUT isolation")

    trusted = tuple(
        _str_list(top.get("trusted_elicitation_clients"), f"{accounts_path}:trusted_elicitation_clients")
    )

    return Config(
        accounts=accounts,
        sync_if_older_than=parse_duration(top.get("sync_if_older_than", "5m")),
        attachments=attachments,
        trusted_elicitation_clients=trusted,
        config_dir=cdir,
        data_dir=ddir,
    )
