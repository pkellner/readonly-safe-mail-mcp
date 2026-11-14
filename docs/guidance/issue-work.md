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
| 2026-11-13 | 054a919 | MacBookM4 | fast suite (issue #1 merge test) | 1498 passed, 1 skipped, 1 xfailed | 21.7 s (load 2.3 to 7.5) |
| 2026-11-13 | e258911 | GitHub CI | full matrix (issue #1 PR #10) | success | about 3 min |
| 2026-11-13 | 6396c73 | MacBookM4 | fast suite | 227 passed, 1 skipped, 1 xfailed | 3.88 s (load 1.6) |
| 2026-11-12 | 922b84a | GitHub CI | full matrix | success | 10 min 34 s |
| 2026-11-12 | e615b46 | GitHub CI | full matrix | success | 3 min 45 s |

## Preview

There is no web app. A preview is the merge-test tree's own install, run beside the op's
installed copy without touching it:

- `RSMM_CONFIG_DIR` and `RSMM_DATA_DIR` point at scratch folders holding a placeholder
  `accounts.yaml`; `readonly-safe-mail-mcp paths` shows which folders are in use.
- It never reads the op's config, index or Keychain entry.

Steps (first used for issue #1, 2026-11-13), from the merge-test worktree after
`uv venv && uv pip install -e ".[dev]"`:

1. Make two new, empty scratch folders `<scratch>/cfg` and `<scratch>/data` (never reuse or
   delete a folder that holds anything).
2. Run [`preview.py`](preview.py) with the worktree's interpreter:
   `RSMM_CONFIG_DIR=<scratch>/cfg RSMM_DATA_DIR=<scratch>/data
   PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring .venv/bin/python -I docs/guidance/preview.py`.
   It writes a placeholder `accounts.yaml` (contacts and calendars on), stores placeholder
   cards and events through the same functions the DAV sync uses, expands them with the
   production worker, marks both sources as just synced so no tool call goes to the network,
   then starts the installed `readonly-safe-mail-mcp serve` over stdio and calls the tools.
   The fail keyring backend keeps every credential lookup away from the op's Keychain.
3. Check the output: the tool list, one contact and one event read back, the untrusted-content
   lines, the hidden code, the invitation label, the incomplete event named, and the
   out-of-window note.

The DAV fetch itself is not in the preview: it needs a server, and the tests cover it against
`tests/fakedav.py`. Mail is not in it either.

## Live steps

Not written down yet. What is known:

- The version is in `pyproject.toml`, `src/readonly_safe_mail_mcp/__init__.py`, `install.sh`
  (`VERSION=`), the tarball line of `README.md`, and the `@v` lines of `docs/THREAT_MODEL.md`
  and `docs/PLAN.md`; `tests/test_repo_hygiene.py` checks they agree and pins the number.
- A release is the tag `v<version>`; pushing `main` and pushing a tag each need the op's own
  go (`AGENTS.md`, Branches and environments).
- The op's installed copy is updated with `uv tool install --force`, and the password must
  be stored by the copy that will read it (`README.md:152-156`). Only the op can do that.
- A check against the real mail account, or any other live account, needs the stored
  password and so may need the op. Whether a worktree's interpreter can read the Keychain
  entry has not been tested.

Ask the op for the release steps at Prompt and merge of the first issue, and write them here.
