"""Unicode normalization (PLAN.md section 4.1, step 3) and the private sentinel
characters the sanitizer uses to mark hidden and quoted regions.

Two forms:
  display_form  what may be shown: NFKC, control/format/surrogate chars removed.
  match_form    what classification sees: additionally decomposed, combining
                marks removed, dashes unified, case-folded, whitespace collapsed.
                Used ONLY for matching, never shown.
"""

from __future__ import annotations

import re
import unicodedata

# ASCII control characters chosen as region markers. Attacker-supplied copies are
# stripped from input before the sanitizer inserts its own (see strip_sentinels).
QUOTE_OPEN, QUOTE_CLOSE, HIDDEN_OPEN, HIDDEN_CLOSE = "\x02", "\x03", "\x04", "\x05"
SENTINELS = QUOTE_OPEN + QUOTE_CLOSE + HIDDEN_OPEN + HIDDEN_CLOSE
_SENTINEL_TABLE = str.maketrans("", "", SENTINELS)

# Every ASCII control except \t, \n and the sentinels (\r is converted to \n first).
_ASCII_CONTROL_RE = re.compile(r"[\x00\x01\x06-\x08\x0b-\x1f\x7f]")
_DROP_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})
_KEEP = frozenset("\n\t" + SENTINELS)


def strip_sentinels(text: str) -> str:
    return text.translate(_SENTINEL_TABLE)


def display_form(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.isascii():
        return _ASCII_CONTROL_RE.sub("", text)
    text = unicodedata.normalize("NFKC", text)
    return "".join(c for c in text if c in _KEEP or unicodedata.category(c) not in _DROP_CATEGORIES)


def _match_char(c: str) -> str:
    if c.isspace():
        return " "
    cat = unicodedata.category(c)
    if cat in ("Mn", "Me", "Cc", "Cf", "Cs"):
        return ""
    if cat == "Pd":
        return "-"
    return c


def match_form(text: str) -> str:
    text = strip_sentinels(text or "")
    if text.isascii():
        text = _ASCII_CONTROL_RE.sub("", text)
    else:
        text = "".join(_match_char(c) for c in unicodedata.normalize("NFKD", text))
    return " ".join(text.casefold().split())
