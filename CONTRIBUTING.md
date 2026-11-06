# Contributing

```sh
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest -q
.venv/bin/ruff check src tests && .venv/bin/ruff format src tests
```

Ground rules:

- **No real mail in the repo.** Fixtures use placeholder addresses (`example.org`,
  `*.example`). `tests/test_repo_hygiene.py` fails on anything else. Locally you can also
  set `RSMM_PRIVATE_GUARD` to a file of your own name, address, and phone, one per line,
  and the tests will fail if any of them appear in tracked files.
- **Never widen the IMAP allowlist** (`imap.check_command`) without a test that asserts
  the exact command strings sent over the wire.
- **Sanitizer changes bump `SANITIZER_VERSION`**, which rebuilds every user's index, and
  come with adversarial tests: zero-width characters, entities, encoded URLs, hidden
  HTML, and quoted-printable splits.
- **Attachment parsing happens only in `extract.py`**, which runs inside the sandbox.
  Never parse attachment bytes in the server process.
- Keep tool outputs compact. They count against the user's AI context.
- Security-sensitive reports go through [SECURITY.md](SECURITY.md), not public issues.
