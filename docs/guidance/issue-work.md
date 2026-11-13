# Issue work: this repo's mechanics

The process is `~/repos/agent-guidance/shared/issues-and-prs.md`. This file holds the
commands and facts that process asks each repo for. Where something is not decided yet it
says so; ask the op once and write the answer here.

## Machine prefixes

| Machine | Prefix |
|---|---|
| MacBookM4 (Apple M4 Max) | `macbookm4` (op, 2026-11-13) |

A machine not listed here: ask the op once, offering the machine's own name, and add a row.

## Worktree setup

From `CONTRIBUTING.md`, in each worktree:

```sh
uv venv && uv pip install -e ".[dev]"
```

Nothing is linked or copied from the primary checkout: the repo has no `.env`, and personal
config is never in it (`.gitignore`).

## Tests

- **Kinds:** unit tests only (pytest). No browser tests, no device profiles.
- **Fast suite:** `.venv/bin/pytest -q`
- **Lint:** `.venv/bin/ruff check src tests && .venv/bin/ruff format src tests`
- **Targeted run:** `.venv/bin/pytest -q tests/<file>.py`, or one test with `-k <name>`.
- **Smoke set:** the fast suite; it is short enough.
- **Per-change measurement checks:** none.
- **Release check:** none. No budget or timing runs exist.
- **Test ports:** none are fixed. The fake IMAP server binds port 0 (`tests/fakeimap.py:154`),
  so worktrees cannot collide. A new fake server must do the same.
- **Lock:** `<scratchpad>/rsmm-suite.lock`.
- **CI:** `.github/workflows/ci.yml`, three operating systems × Python 3.11 to 3.13, plus
  ruff. A sandbox or child-process change must be read on all three: Windows has no
  sandbox runner.

### Timings history

| Date | Commit | Machine | Run | Result | Duration |
|---|---|---|---|---|---|
| 2026-11-13 | 6396c73 | MacBookM4 | fast suite | 227 passed, 1 skipped, 1 xfailed | 3.88 s (load 1.6) |
| 2026-11-12 | 922b84a | GitHub CI | full matrix | success | 10 min 34 s |
| 2026-11-12 | e615b46 | GitHub CI | full matrix | success | 3 min 45 s |

## Preview

There is no web app. A preview is the merge-test tree's own install, run beside the op's
installed copy without touching it:

- `RSMM_CONFIG_DIR` and `RSMM_DATA_DIR` point at scratch folders holding a placeholder
  `accounts.yaml`; `readonly-safe-mail-mcp paths` shows which folders are in use.
- It never reads the op's config, index or Keychain entry.

Not exercised yet (2026-11-13): the first issue to use it writes down the exact steps here.

## Live steps

Not written down yet. What is known:

- The version is in three places: `pyproject.toml:3`, `install.sh:23`, `README.md:78`.
- A release is the tag `v<version>`; pushing `main` and pushing a tag each need the op's own
  go (`AGENTS.md`, Branches and environments).
- The op's installed copy is updated with `uv tool install --force`, and the password must
  be stored by the copy that will read it (`README.md:152-156`). Only the op can do that.
- A check against the real mail account, or any other live account, needs the stored
  password and so may need the op. Whether a worktree's interpreter can read the Keychain
  entry has not been tested.

Ask the op for the release steps at Prompt and merge of the first issue, and write them here.
