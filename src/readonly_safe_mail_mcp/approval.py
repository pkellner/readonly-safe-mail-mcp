"""Human approval for opening attachments (PLAN.md section 7.1).

The default channel is a native dialog owned by this server, so the decision comes from
the person at the computer and not from the AI client. Item text is passed to the dialog
as arguments, never pasted into script source. MCP elicitation is used only for clients
the user lists in `trusted_elicitation_clients`.

Anything other than an explicit approval (deny, cancel, close, timeout, malformed reply,
no dialog available) approves nothing.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass

DIALOG_TIMEOUT = 180
MAX_LABEL = 160

_OSASCRIPT = """
on run argv
  set promptText to item 1 of argv
  set theItems to items 2 thru (count of argv) of argv
  activate
  set picked to choose from list theItems with title "Email attachments" with prompt promptText ¬
    default items theItems OK button name "Open selected" cancel button name "Deny" ¬
    with multiple selections allowed and empty selection allowed
  if picked is false then return ""
  set AppleScript's text item delimiters to linefeed
  return picked as text
end run
"""

PROMPT = (
    "Your AI assistant asks to open these email attachments. Selected items are read in a "
    "sandbox and their text or image is sent to the assistant. Deselect anything you don't want "
    "shared."
)


IMAGE_NOTE = (
    " Images are sent as pictures and cannot be redacted: any code, QR code or password visible "
    "in an image reaches the assistant."
)


class NoApprovalChannel(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    label: str  # e.g. "2026-11-04 · Angela Ruiz · Budget draft · budget.pdf (120 KB)"
    image: bool = False


def prompt_for(cands: list[Candidate]) -> str:
    return PROMPT + (IMAGE_NOTE if any(c.image for c in cands) else "")


def _clean(s: str) -> str:
    s = " ".join(str(s).split())  # no newlines: they would split one item into two
    return s if len(s) <= MAX_LABEL else s[: MAX_LABEL - 1] + "…"


def labels(cands: list[Candidate]) -> list[str]:
    return [f"{i}. {_clean(c.label)}" for i, c in enumerate(cands, 1)]


def _parse_selection(out: str, n: int) -> set[int]:
    chosen = set()
    for line in out.splitlines():
        head = line.split(".", 1)[0].strip()
        if head.isdigit() and 1 <= int(head) <= n:
            chosen.add(int(head) - 1)
    return chosen


def channel_available() -> bool:
    if sys.platform == "darwin":
        return shutil.which("osascript") is not None
    if sys.platform.startswith("linux"):
        return shutil.which("zenity") is not None
    return False


def ask_native(cands: list[Candidate], *, timeout: float = DIALOG_TIMEOUT) -> set[int]:
    """Show the dialog. Returns the indexes the human approved (empty = nothing)."""
    items = labels(cands)
    if sys.platform == "darwin" and shutil.which("osascript"):
        argv = ["osascript", "-e", _OSASCRIPT, prompt_for(cands), *items]
    elif sys.platform.startswith("linux") and shutil.which("zenity"):
        argv = [
            "zenity",
            "--list",
            "--checklist",
            "--title=Email attachments",
            f"--text={prompt_for(cands)}",
            "--column=Open",
            "--column=Attachment",
            "--separator=\n",
            "--ok-label=Open selected",
            "--cancel-label=Deny",
            "--width=800",
            "--height=400",
        ]
        for it in items:
            argv += ["TRUE", it]
    else:
        raise NoApprovalChannel(
            "no approval dialog is available on this system, so attachments can't be opened"
        )
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return set()
    if proc.returncode != 0:
        return set()  # Deny, closed, or error
    return _parse_selection(proc.stdout, len(cands))


async def ask_elicitation(ctx, cands: list[Candidate]) -> set[int]:
    """For trusted clients only: the client shows the request and reports the choice."""
    from pydantic import Field, create_model

    fields = {
        f"item_{i}": (bool, Field(default=True, description=lab)) for i, lab in enumerate(labels(cands), 1)
    }
    model = create_model("ApproveAttachments", **fields)  # type: ignore[call-overload]
    try:
        res = await ctx.elicit(prompt_for(cands), model)
    except Exception:
        return set()
    if getattr(res, "action", None) != "accept" or res.data is None:
        return set()
    return {i - 1 for i in range(1, len(cands) + 1) if getattr(res.data, f"item_{i}", False) is True}


def client_name(ctx) -> str:
    try:
        return ctx.request_context.session.client_params.client_info.name or ""
    except Exception:
        return ""
