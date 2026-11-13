"""Run a worker in an isolated child process (PLAN.md section 7.2): the attachment
extraction worker (extract.py) or the recurrence expansion worker (expand.py).

The worker gets: no network, no credentials (empty environment, no Keychain/keyring
services, no config or data directory), read access only to the Python runtime and
installed packages, no file writes, CPU/time/output limits, and one request on stdin.

  macOS:  sandbox-exec with a deny-by-default profile
  Linux:  bubblewrap (bwrap) with every namespace unshared
  other:  attachments refused, unless config `attachments: ask-unsandboxed`; the
          expansion worker runs as a plain child with the same limits
A timeout alone is never called a sandbox.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import site
import subprocess
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path

from . import paths

TIMEOUT = 60
CPU_SECONDS = 40
MAX_REPLY_BYTES = 40_000_000
EXTRACT = "readonly_safe_mail_mcp.extract"


class IsolationUnavailable(RuntimeError):
    pass


class WorkerError(RuntimeError):
    pass


class WorkerTimeout(WorkerError):
    """The worker was killed at its wall-clock deadline."""


@dataclass(frozen=True)
class Runner:
    name: str  # "sandbox-exec", "bwrap", or "unsandboxed"
    argv_prefix: tuple[str, ...]


def _forbidden() -> list[Path]:
    """Directories the worker must never be able to read, even if under an allowed path."""
    out = [paths.config_dir(), paths.data_dir(), Path.home() / ".ssh"]
    if sys.platform == "darwin":
        out.append(Path.home() / "Library" / "Keychains")
    return [p.resolve() for p in out]


def _runtime_paths() -> list[Path]:
    """Python runtime + installed packages (+ this package's source, for editable installs)."""
    cands = {
        Path(sys.prefix),
        Path(sys.base_prefix),
        Path(sysconfig.get_paths()["stdlib"]),
        Path(sysconfig.get_paths()["platstdlib"]),
        Path(__file__).resolve().parent.parent,  # the directory containing this package
        Path(sys.executable).resolve().parent.parent,
        *(Path(p) for p in site.getsitepackages()),
    }
    home = Path.home().resolve()
    forbidden = _forbidden()
    out = []
    for p in cands:
        try:
            p = p.resolve()
        except OSError:
            continue
        if not p.exists() or p == home or p == Path("/"):
            continue
        if any(f == p or p in f.parents for f in forbidden):
            continue  # allowing p would expose a forbidden directory
        out.append(p)
    return sorted(set(out))


def _sb_quote(p: Path | str) -> str:
    return '"' + str(p).replace("\\", "\\\\").replace('"', '\\"') + '"'


def macos_profile() -> str:
    reads = " ".join(f"(subpath {_sb_quote(p)})" for p in _runtime_paths())
    # python.org framework builds re-exec Python.app inside the framework.
    execs = f"(subpath {_sb_quote(Path(sys.base_prefix).resolve())})"
    denies = " ".join(f"(subpath {_sb_quote(p)})" for p in _forbidden())
    return f"""(version 1)
(deny default)
(allow process-exec (literal {_sb_quote(sys.executable)})
  (literal {_sb_quote(Path(sys.executable).resolve())}) {execs})
(allow file-read-metadata)
(allow file-read* {reads}
  (literal "/") (subpath "/System") (subpath "/usr/lib") (subpath "/usr/share")
  (subpath "/private/var/db/dyld") (subpath "/Library/Apple/System")
  (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random")
  (literal "/private/etc/localtime") (subpath "/private/var/db/timezone"))
(deny file-read* {denies})
(allow file-write-data (literal "/dev/null"))
(allow sysctl-read)
"""


def find_runner(mode: str, worker_module: str = EXTRACT) -> Runner:
    """mode: the config `attachments` value. "ask-unsandboxed" gives a plain child where no
    sandbox is available; the runner is then named "unsandboxed", never a sandbox."""
    if mode == "never":
        raise IsolationUnavailable("opening attachments is turned off in config (attachments: never)")
    py = sys.executable  # not resolved: a venv's python finds its packages through pyvenv.cfg
    worker = (py, "-I", "-m", worker_module)
    if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists():
        return Runner("sandbox-exec", ("/usr/bin/sandbox-exec", "-p", macos_profile(), *worker))
    bwrap = shutil.which("bwrap") if sys.platform.startswith("linux") else None
    if bwrap:
        args = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv"]
        for p in ("/usr", "/lib", "/lib64", "/bin", "/etc/ld.so.cache", "/etc/localtime"):
            if Path(p).exists():
                args += ["--ro-bind", p, p]
        for p in _runtime_paths():
            args += ["--ro-bind", str(p), str(p)]
        args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
        return Runner("bwrap", (*args, *worker))
    if mode == "ask-unsandboxed":
        return Runner("unsandboxed", worker)
    raise IsolationUnavailable(
        "no sandbox is available on this system (macOS sandbox-exec or Linux bubblewrap), so "
        "attachments are not opened. Install bubblewrap, or set `attachments: ask-unsandboxed` "
        "in accounts.yaml to accept the risk."
    )


def _limits(cpu_seconds: int) -> None:  # runs in the child before exec
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    except Exception:
        pass


def call_worker(runner: Runner, request: bytes, *, timeout: float, cpu_seconds: int = CPU_SECONDS) -> dict:
    """Send one request to a worker child and return its reply object, which has "ok".
    Raises WorkerTimeout when the child is killed at `timeout`, WorkerError for any other
    reply that is not a JSON object with "ok"."""
    try:
        proc = subprocess.run(
            list(runner.argv_prefix),
            input=request,
            capture_output=True,
            timeout=timeout,
            env={"LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"},  # nothing inherited: no secrets
            cwd="/",
            close_fds=True,
            preexec_fn=functools.partial(_limits, cpu_seconds) if os.name == "posix" else None,
        )
    except subprocess.TimeoutExpired:
        raise WorkerTimeout("took too long and was stopped") from None
    if len(proc.stdout) > MAX_REPLY_BYTES:
        raise WorkerError("produced too much output")
    try:
        reply = json.loads(proc.stdout or b"{}")
    except ValueError:
        reply = {}
    if not isinstance(reply, dict) or "ok" not in reply:
        raise WorkerError(f"reader failed (exit {proc.returncode})")
    return reply


def run_worker(
    runner: Runner, kind: str, raw: bytes, encoding: str, charset: str | None, *, timeout: float = TIMEOUT
) -> list[dict]:
    header = json.dumps({"kind": kind, "encoding": encoding, "charset": charset}).encode() + b"\n"
    try:
        reply = call_worker(runner, header + raw, timeout=timeout)
    except WorkerTimeout:
        raise WorkerError("opening the attachment took too long and was stopped") from None
    except WorkerError as e:
        raise WorkerError(f"attachment {e}") from None
    if not reply["ok"]:
        raise WorkerError(str(reply.get("error", "attachment could not be read")))
    return list(reply.get("items", []))
