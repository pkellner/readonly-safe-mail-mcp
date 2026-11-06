"""Command-line entry point.

readonly-safe-mail-mcp                 run the MCP server over stdio (default)
readonly-safe-mail-mcp set-password A  store account A's password in the OS keyring
readonly-safe-mail-mcp print-rules     show the effective merged rules per account
readonly-safe-mail-mcp paths           show config and data directories
readonly-safe-mail-mcp doctor          check config and credentials
readonly-safe-mail-mcp sync            sync mail into the local index
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

import yaml

from . import __version__, credentials, paths
from .config import ConfigError, load_config

NOT_YET = "not implemented yet (see docs/PLAN.md build order)"


def _cmd_serve(_args) -> int:
    print(f"MCP server {NOT_YET}", file=sys.stderr)
    return 2


def _cmd_set_password(args) -> int:
    cfg = load_config()
    if args.account not in cfg.accounts:
        print(f"Unknown account {args.account!r}. Known: {sorted(cfg.accounts)}", file=sys.stderr)
        return 1
    pw = getpass.getpass(f"App-specific password for {cfg.accounts[args.account].username}: ")
    if not pw:
        print("Empty password; nothing stored.", file=sys.stderr)
        return 1
    credentials.set_password(args.account, pw)
    print(
        f"Stored password for account {args.account!r} in the OS keyring (service {credentials.SERVICE!r})."
    )
    return 0


def _cmd_print_rules(args) -> int:
    cfg = load_config()
    names = [args.account] if args.account else sorted(cfg.accounts)
    for name in names:
        acct = cfg.accounts[name]
        print(f"# account: {name}  ({acct.username} @ {acct.host}:{acct.port})")
        print(f"# folders: allow={list(acct.folders.allow)} deny={list(acct.folders.deny)}")
        print(yaml.safe_dump(acct.rules.as_dict(), sort_keys=False))
    return 0


def _cmd_paths(_args) -> int:
    print(f"config: {paths.config_dir()}")
    print(f"data:   {paths.data_dir()}")
    return 0


def _cmd_doctor(_args) -> int:
    ok = True
    cfg = load_config()
    print(f"config dir: {cfg.config_dir}")
    print(f"data dir:   {cfg.data_dir}", end="")
    if paths.looks_synced(cfg.data_dir):
        print("  WARNING: looks like a cloud-synced folder; the index must stay local")
        ok = False
    else:
        print()
    print(f"attachments: {cfg.attachments}   sync_if_older_than: {cfg.sync_if_older_than}s")
    for name, acct in cfg.accounts.items():
        try:
            credentials.get_password(name)
            cred = "password found"
        except credentials.CredentialError as e:
            cred, ok = f"MISSING ({e})", False
        print(f"- {name}: {acct.username} @ {acct.host}:{acct.port} — {cred}")
    print(f"connection test {NOT_YET}")
    return 0 if ok else 1


def _cmd_sync(_args) -> int:
    print(f"sync {NOT_YET}", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="readonly-safe-mail-mcp", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server over stdio (default)").set_defaults(fn=_cmd_serve)
    sp = sub.add_parser("set-password", help="store an account password in the OS keyring")
    sp.add_argument("account")
    sp.set_defaults(fn=_cmd_set_password)
    pr = sub.add_parser("print-rules", help="show effective merged rules")
    pr.add_argument("--account")
    pr.set_defaults(fn=_cmd_print_rules)
    sub.add_parser("paths", help="show config and data directories").set_defaults(fn=_cmd_paths)
    sub.add_parser("doctor", help="check config and credentials").set_defaults(fn=_cmd_doctor)
    sub.add_parser("sync", help="sync mail into the local index").set_defaults(fn=_cmd_sync)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Logs go to stderr: stdout is the MCP transport when serving.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    fn = getattr(args, "fn", _cmd_serve)
    try:
        return fn(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
