"""Command-line entry point.

readonly-safe-mail-mcp                 run the MCP server over stdio (default)
readonly-safe-mail-mcp set-password A  store account A's password in the OS keyring
readonly-safe-mail-mcp print-rules     show the effective merged rules per account
readonly-safe-mail-mcp paths           show config and data directories
readonly-safe-mail-mcp check           check config, credentials, and a read-only connection
readonly-safe-mail-mcp sync            sync mail into the local index (shows progress)
readonly-safe-mail-mcp status          sync status and message counts (offline)
"""

from __future__ import annotations

import argparse
import getpass
import logging
import ssl
import sys

import yaml

from . import __version__, credentials, paths
from .config import Account, ConfigError, load_config
from .imap import ImapError, MailSource
from .sanitize import parse_headers


def _cmd_serve(_args) -> int:
    from .server import serve

    serve()
    return 0


def _cmd_delete_password(args) -> int:
    import keyring.errors

    try:
        credentials.delete_password(args.account)
    except keyring.errors.PasswordDeleteError:
        print(f"No stored password for account {args.account!r}.", file=sys.stderr)
        return 1
    print(f"Removed password for account {args.account!r} from the OS keyring.")
    return 0


def _cmd_set_password(args) -> int:
    cfg = load_config()
    if args.account not in cfg.accounts:
        print(f"Unknown account {args.account!r}. Known: {sorted(cfg.accounts)}", file=sys.stderr)
        return 1
    if not sys.stdin.isatty():
        print(
            "set-password needs a real terminal so the password is not echoed. "
            "Run it in Terminal.app (or another terminal window).",
            file=sys.stderr,
        )
        return 2
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


def _connection_check(acct: Account, password: str) -> tuple[bool, list[str]]:
    """Connect (verified TLS), list folders, EXAMINE one permitted folder, read the
    newest message's headers. Read-only; nothing else is fetched."""
    lines: list[str] = []
    try:
        with MailSource.open(acct, password) as src:
            folders = src.list_folders()
            permitted = [name for name, _, ok in folders if ok]
            lines.append(f"TLS verified; logged in; {len(folders)} folders, {len(permitted)} permitted")
            target = "INBOX" if "INBOX" in permitted else (permitted[0] if permitted else None)
            if target is None:
                lines.append("no permitted folders: check `folders:` in accounts.yaml")
                return False, lines
            info = src.examine(target)
            # Search a recent UID window: imaplib caps a response line at 1 MB.
            window = max(1, (info.uidnext or 1) - 500) if info.uidnext else None
            uids = src.search_uids(min_uid=window) if window else src.search_uids()
            lines.append(f"{target}: {info.exists} messages (opened read-only)")
            if uids:
                hdr = src.fetch_headers([uids[-1]])
                if hdr:
                    view, _, _ = parse_headers(hdr[0].header_bytes)
                    lines.append(f"newest: {view.date} | {view.from_} | {view.subject}")
        return True, lines
    except ssl.SSLCertVerificationError as e:
        return False, lines + [
            f"TLS certificate verification FAILED ({e.verify_message}); password was not sent"
        ]
    except (ImapError, OSError) as e:
        return False, lines + [f"FAILED: {e}"]


def _cmd_check(_args) -> int:
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
        print(f"- {name}: {acct.username} @ {acct.host}:{acct.port}")
        try:
            password = credentials.get_password(name)
        except credentials.CredentialError as e:
            print(f"    password MISSING: {e}")
            ok = False
            continue
        good, lines = _connection_check(acct, password)
        ok = ok and good
        for line in lines:
            print(f"    {line}")
    return 0 if ok else 1


def _cmd_sync(args) -> int:
    import threading
    import time

    from .sync import SyncBusy, sync_all

    cfg = load_config()
    if paths.looks_synced(cfg.data_dir):
        print(f"Refusing to sync: {cfg.data_dir} looks like a cloud-synced folder.", file=sys.stderr)
        return 1
    if args.account and args.account not in cfg.accounts:
        print(f"Unknown account {args.account!r}. Known: {sorted(cfg.accounts)}", file=sys.stderr)
        return 1
    mu, last = threading.Lock(), [0.0]

    def progress(account: str, folder: str, done: int, total: int) -> None:
        now = time.monotonic()
        with mu:
            if done in (0, total) or now - last[0] >= 2:
                last[0] = now
                print(f"  {account}/{folder}: {done:,} / {total:,}", file=sys.stderr, flush=True)

    print("Syncing (newest mail first; safe to interrupt and resume)...", file=sys.stderr)
    stop = threading.Event()
    try:
        reports = sync_all(
            cfg, accounts=[args.account] if args.account else None, progress=progress, stop=stop
        )
    except SyncBusy:
        print("Another sync is already running (CLI or MCP server). Try again later.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        stop.set()
        print("Interrupted; progress is saved. Run sync again to continue.", file=sys.stderr)
        return 130
    ok = True
    for r in reports:
        if r.error:
            ok = False
            print(f"{r.account}: FAILED: {r.error}")
        else:
            print(f"{r.account}: +{r.added:,} indexed, -{r.removed:,} removed, {len(r.folders)} folders")
    return 0 if ok else 1


def _cmd_status(_args) -> int:
    from . import index
    from .query import status_lines

    cfg = load_config()
    path = index.index_path(cfg)
    db = index.connect(path, create=False)
    try:
        for line in status_lines(index.account_status(db, sorted(cfg.accounts)), cfg.sync_if_older_than):
            print(line)
    finally:
        if db:
            db.close()
    print(f"index: {path}" + ("" if path.exists() else " (not created yet: run sync)"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="readonly-safe-mail-mcp", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server over stdio (default)").set_defaults(fn=_cmd_serve)
    sp = sub.add_parser("set-password", help="store an account password in the OS keyring")
    sp.add_argument("account")
    sp.set_defaults(fn=_cmd_set_password)
    dp = sub.add_parser("delete-password", help="remove an account password from the OS keyring")
    dp.add_argument("account")
    dp.set_defaults(fn=_cmd_delete_password)
    pr = sub.add_parser("print-rules", help="show effective merged rules")
    pr.add_argument("--account")
    pr.set_defaults(fn=_cmd_print_rules)
    sub.add_parser("paths", help="show config and data directories").set_defaults(fn=_cmd_paths)
    sub.add_parser("check", help="check config, credentials, and a read-only connection").set_defaults(
        fn=_cmd_check
    )
    sy = sub.add_parser("sync", help="sync mail into the local index")
    sy.add_argument("--account")
    sy.set_defaults(fn=_cmd_sync)
    sub.add_parser("status", help="show sync status and indexed message counts (offline)").set_defaults(
        fn=_cmd_status
    )
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
