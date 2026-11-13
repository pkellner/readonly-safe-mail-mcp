# Agent guidance for this repo

General guidance for every repo is in `~/repos/agent-guidance/shared/`, read in place:
`working-agreement.md` always, and `issues-and-prs.md` before filing an issue or making a PR
for one. This file holds what is particular to this repo. Commands and procedure are in
`docs/guidance/issue-work.md`.

## Branches and environments

- **Base branch: `main`** (op, 2026-11-13). Issue work merges into it through an integration
  branch, as `issues-and-prs.md` says.
- **A push to `main` is a deploy.** The public one-line installer and uninstaller are fetched
  from `main` (`README.md:39, 56`; `install.sh:24`), so a pushed change to `install.sh` or
  `uninstall.sh` reaches every new install at once. It needs the op's own go each time.
- **A release is a tag.** The package users install is the tarball of tag `v<version>`
  (`install.sh:23-25`, `README.md:78`). Creating or pushing a tag needs the op's own go.
- **The repo is public.** No personal data in tracked files, issues or PRs: no real
  addresses, names, mail content or local paths. `tests/test_repo_hygiene.py` checks tracked
  files for addresses; nothing checks issues and PRs.
- **The live system is the op's installed copy**: the `uv tool` install, its config folder,
  its index and its Keychain entry. Development never reads or writes them. Run a
  development copy with `RSMM_CONFIG_DIR` and `RSMM_DATA_DIR` set to scratch folders
  (`paths.py:17, 27`).

## Rules

- `CONTRIBUTING.md`'s ground rules bind agents: no real mail in the repo, never widen the
  IMAP allowlist without a wire-level test, a sanitizer change bumps `SANITIZER_VERSION`,
  attachment bytes are parsed only in the sandbox, tool output stays compact.
- Read `docs/THREAT_MODEL.md` before changing anything that fetches, sanitizes, indexes or
  returns content.
- A bump of `SANITIZER_VERSION` or `SCHEMA_VERSION` discards every user's mail index
  (`index.py:27, 82`). Name it in the issue and the PR; never do it as a side effect.
