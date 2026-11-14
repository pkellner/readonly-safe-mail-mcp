"""Command-line entry point.

readonly-safe-mail-mcp                 run the MCP server over stdio (default)
readonly-safe-mail-mcp set-password A  store account A's password (OS keyring, or --store file)
readonly-safe-mail-mcp print-rules     show the effective merged rules per account
readonly-safe-mail-mcp paths           show config and data directories
readonly-safe-mail-mcp check           check config, credentials, and a read-only login per source
readonly-safe-mail-mcp sync            sync mail, then contacts and calendars, into the local indexes
readonly-safe-mail-mcp status          sync status and counts per source (offline)
"""

from __future__ import annotations

import argparse
import getpass
import logging
import ssl
import subprocess
import sys

import yaml

from . import __version__, credentials, davindex, davsync, paths
from .config import Account, ConfigError, load_config
from .dav import DavError
from .imap import ImapError, MailSource
from .sanitize import parse_headers


def _cmd_serve(_args) -> int:
    from .server import serve

    serve()
    return 0


def _cmd_delete_password(args) -> int:
    removed = credentials.delete_password(args.account)
    if not removed:
        print(f"No stored password for account {args.account!r}.", file=sys.stderr)
        return 1
    print(f"Removed the password for {args.account!r} from: {', '.join(removed)}.")
    print("To invalidate it everywhere, also revoke the app password at your mail provider.")
    return 0


PBPASTE = "/usr/bin/pbpaste"
PBCOPY = "/usr/bin/pbcopy"


def _clipboard_text() -> str:
    """One line of text from the macOS clipboard, trimmed. '' if there is none,
    it spans several lines (not a password), or this is not a Mac."""
    if sys.platform != "darwin":
        return ""
    try:
        out = subprocess.run([PBPASTE], capture_output=True, timeout=5, check=True)
        text = out.stdout.decode("utf-8").strip()
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return ""
    if not text or any(c in text for c in "\r\n"):
        return ""
    return text


def _clear_clipboard() -> bool:
    try:
        subprocess.run([PBCOPY], input=b"", timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


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
    who = cfg.accounts[args.account].username
    pw, from_clipboard = "", False
    clip = _clipboard_text()
    if clip:
        # The length is shown so the user can tell it is the password they just
        # copied. The text itself is never printed.
        print(f"Your clipboard holds {len(clip)} characters. How should the password for {who} be entered?")
        print("  1) Use what is in the clipboard")
        print("  2) Type or paste it myself")
        choice = ""
        while choice not in ("1", "2"):
            try:
                choice = input("Choose 1 or 2, then press Return: ").strip()
            except EOFError:
                print("\nNothing stored.", file=sys.stderr)
                return 1
        if choice == "1":
            pw, from_clipboard = clip, True
    if not pw:
        print("Nothing appears on screen while you type or paste. That is normal. Press Return when done.")
        pw = getpass.getpass(f"App-specific password for {who}: ")
    if not pw:
        print("Empty password; nothing stored.", file=sys.stderr)
        return 1
    where = credentials.set_password(args.account, pw, backend=args.store)
    print(f"Stored password ({len(pw)} characters) for account {args.account!r} in {where}.")
    if from_clipboard and _clear_clipboard():
        print("Cleared the clipboard, so the password is not left there.")
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


def _dav_check(acct: Account, password: str) -> tuple[bool, list[str]]:
    """Log in to each DAV source that is on and count its address books or calendars, one
    line per source. Through the guarded connection (dav.py): PROPFIND only, nothing else
    is sent, and no card or event is fetched."""
    lines: list[str] = []
    ok = True
    sources = {
        davindex.CONTACTS: ("CardDAV", "address book"),
        davindex.CALENDARS: ("CalDAV", "calendar"),
    }
    try:
        with davsync.open_dav_source(acct, password) as src:
            for source in davsync.sources_on(acct):
                protocol, noun = sources[source]
                try:
                    found = src.collections(src.home(source))
                    if any(it.status != 200 for it in found):
                        raise DavError(f"{noun} list incomplete (an entry without status 200)")
                    n = len(davsync.wanted_collections(acct, source, found))
                    lines.append(
                        f"{source}: logged in over {protocol}; {n} {noun}(s); only read requests sent"
                    )
                    if source == davindex.CALENDARS and acct.calendars is not None:
                        types = [(it.displayname or "", it.resource_types or ()) for it in found]
                        unmatched = acct.calendars.unmatched([name for name, t in types if "calendar" in t])
                        if unmatched:
                            lines.append(
                                f"calendars: allow/deny name(s) matching no calendar: {', '.join(unmatched)}"
                            )
                except DavError as e:
                    ok = False
                    lines.append(f"{source}: FAILED: {e}")
    except DavError as e:
        return False, [f"FAILED: {e}"]
    return ok, lines


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
    try:
        from .isolate import IsolationUnavailable, find_runner

        print(f"attachment sandbox: {find_runner(cfg.attachments).name}")
    except IsolationUnavailable as e:
        print(f"attachment sandbox: none ({e})")
    for w in permission_warnings(cfg):
        print(f"WARNING: {w}")
        ok = False
    for name, acct in cfg.accounts.items():
        print(f"- {name}: {acct.username} @ {acct.host}:{acct.port}")
        try:
            password = credentials.get_password(name)
        except credentials.CredentialError as e:
            print(f"    password MISSING: {e}")
            ok = False
            continue
        good, lines = _connection_check(acct, password)
        if acct.contacts or acct.calendars:
            dav_good, dav_lines = _dav_check(acct, password)
            good, lines = good and dav_good, lines + dav_lines
        ok = ok and good
        for line in lines:
            print(f"    {line}")
    return 0 if ok else 1


def permission_warnings(cfg) -> list[str]:
    """Paths other OS users could read or change (PLAN.md section 3.1)."""
    import os
    import stat
    from pathlib import Path

    if os.name != "posix":
        return []
    out = []
    me = os.getuid()
    code = Path(__file__).resolve().parent
    checks = [
        (cfg.config_dir, False),
        (cfg.config_dir / "accounts.yaml", False),
        (cfg.config_dir / "rules.yaml", False),
        (credentials.credentials_dir(), True),
        (cfg.data_dir, True),
        (code, False),
    ]
    checks += [(p, True) for p in credentials.credentials_dir().glob("*")]
    checks += [(p, False) for p in code.glob("*.py")]
    for path, secret in checks:
        try:
            st = path.stat()
        except OSError:
            continue
        mode = stat.filemode(st.st_mode)
        if st.st_uid != me and path != code and path.parent != code:
            out.append(f"{path} is owned by another user")
        if st.st_mode & 0o022:
            out.append(f"{path} can be changed by other users ({mode})")
        if secret and st.st_mode & 0o044:
            out.append(f"{path} can be read by other users ({mode})")
    return out


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
                where = f"{account}/{folder}" if account else folder
                print(f"  {where}: {done:,} / {total:,}", file=sys.stderr, flush=True)

    import signal

    def _terminate(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)  # pkill / kill stop it the same way Ctrl-C does
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
    rc = _sync_dav(cfg, args.account, progress)
    return rc or (0 if ok else 1)


def _sync_dav(cfg, account: str | None, progress) -> int:
    """The contacts and calendar sync, after the mail sync has finished and released its lock."""
    from .sync import SyncBusy

    words = davindex.source_words(cfg)
    if words:
        print(f"Syncing {words}...", file=sys.stderr)
    try:
        reports = davsync.sync_all(cfg, accounts=[account] if account else None, progress=progress)
    except SyncBusy:
        print(
            f"Another {davindex.source_words(cfg)} sync is already running (CLI or MCP server). "
            "Try again later.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("Interrupted; run sync again to continue.", file=sys.stderr)
        return 130
    ok = True
    for r in reports:
        if r.error:
            ok = False
            print(f"{r.account} {r.source}: FAILED: {r.error}")
        else:
            pending = f"; {r.pending:,} event(s) pending expansion (next sync)" if r.pending else ""
            print(f"{r.account} {r.source}: +{r.added:,} stored, -{r.removed:,} removed{pending}")
    return 0 if ok else 1


def _cmd_status(_args) -> int:
    from . import index
    from .query import source_status_lines, status_lines

    cfg = load_config()
    path = index.index_path(cfg)
    db = index.connect(path, create=False)
    try:
        for line in status_lines(index.account_status(db, sorted(cfg.accounts)), cfg.sync_if_older_than):
            print(line)
    finally:
        if db:
            db.close()
    dav_path = davindex.index_path(cfg)
    if dav_path is not None:
        db = davindex.connect(cfg, create=False)
        try:
            statuses = [
                st
                for source in (davindex.CONTACTS, davindex.CALENDARS)
                for st in davindex.source_status(
                    db, [a.name for a in davindex.dav_accounts(cfg, source)], source
                )
            ]
            for line in source_status_lines(statuses, calendar_years=cfg.calendar_years):
                print(line)
        finally:
            if db:
                db.close()
    print(f"index: {path}" + ("" if path.exists() else " (not created yet: run sync)"))
    if dav_path is not None:
        print(
            f"{davindex.source_words(cfg)} index: {dav_path}"
            + ("" if dav_path.exists() else " (not created yet: run sync)")
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="readonly-safe-mail-mcp", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server over stdio (default)").set_defaults(fn=_cmd_serve)
    sp = sub.add_parser("set-password", help="store an account password in the OS keyring")
    sp.add_argument("account")
    sp.add_argument(
        "--store",
        choices=credentials.BACKENDS,
        default="keyring",
        help="where to keep it: the OS keyring (default) or a mode-600 file (dedicated-user installs)",
    )
    sp.set_defaults(fn=_cmd_set_password)
    dp = sub.add_parser("delete-password", help="remove an account's stored password (keyring and file)")
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
