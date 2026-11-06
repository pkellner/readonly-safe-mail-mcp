"""Personal-data guard: tracked files may only contain placeholder email addresses.

Real mail must never be committed as a test fixture. A new address here fails CI until
it is replaced by a placeholder (example.org, *.example) or added to the allowlist on
purpose. Optional local extra: put one private string per line (your name, address,
phone) in the file named by RSMM_PRIVATE_GUARD; any match fails too.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
PLACEHOLDER_DOMAIN_RE = re.compile(r"(?:^|\.)(?:example(?:\.(?:com|org|net))?|test|invalid|localhost)$", re.I)
# Well-known sender addresses used as fixtures for the security-mail classifier, and the
# generic "you@" / "me@" placeholders in docs.
ALLOWED = {
    "you@icloud.com",
    "you@gmail.com",
    "me@icloud.com",
    "no_reply@id.apple.com",
    "no_reply@appleid.apple.com",
    "noreply@anthropic.com",
}


def tracked_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [ROOT / f for f in out.splitlines() if (ROOT / f).is_file()]


def _text(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def test_only_placeholder_email_addresses():
    bad = []
    for p in tracked_files():
        text = _text(p)
        if text is None:
            continue
        for m in EMAIL_RE.finditer(text):
            addr = m.group(0)
            if addr.lower() in ALLOWED or PLACEHOLDER_DOMAIN_RE.search(m.group(1)):
                continue
            bad.append(f"{p.relative_to(ROOT)}: {addr}")
    assert not bad, "real-looking email addresses in tracked files:\n" + "\n".join(bad)


def test_private_strings_absent():
    guard = os.environ.get("RSMM_PRIVATE_GUARD")
    if not guard or not Path(guard).is_file():
        pytest.skip("RSMM_PRIVATE_GUARD not set")
    needles = [ln.strip().lower() for ln in Path(guard).read_text().splitlines() if ln.strip()]
    hits = []
    for p in tracked_files():
        text = (_text(p) or "").lower()
        hits += [
            f"{p.relative_to(ROOT)}: matches guard line {i + 1}" for i, n in enumerate(needles) if n in text
        ]
    assert not hits, "\n".join(hits)


def test_no_personal_config_tracked():
    bad = [
        str(p.relative_to(ROOT))
        for p in tracked_files()
        if p.name == "accounts.yaml" or p.name.endswith(".local.yaml") or p.suffix in (".sqlite", ".env")
    ]
    assert not bad, f"personal config or data tracked: {bad}"
