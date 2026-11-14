# readonly-safe-mail-mcp — Design Plan (for review)

Status: **built through step 6 (0.2.0), plus contacts and calendars (0.3.0, §14); see §13.** Reviewers: please challenge anything below, especially §3 (threat model) and §11 (questions for reviewers).

## 1. Goal

A local MCP server that lets an AI assistant (Claude, Cursor, VS Code, any MCP client) **search and read** across several personal email accounts. It makes three separate promises, of different strengths:

1. **Structural:** the server exposes no operation that sends or modifies mail. Enforced by the absence of such code and by a narrow IMAP layer (§4.4).
2. **Best effort:** all mail and attachment text passes through credential redaction and security-message classification before it is indexed or returned. Approved images are returned as pixels, with metadata stripped but no redaction (§7.3). Detection is heuristic; see §3 residual risks.
3. **Structural:** attachment content is fetched only after human approval through the configured trusted approval channel (§7).

These hold only if the assistant cannot independently reach the credentials, the index files, or the server's code and rules. **An AI agent with shell or file tools running as your OS user (Claude Code, Cursor agent mode, Codex, etc.) can reach all of them.** §3.1 gives the recommended setup; the README states it prominently.

Example target prompt: *"Find all the emails from {person} where she talks about sending out the donation ask letter, and specifically where she talks about the costs."*

### Non-goals (v1)
- Sending or drafting mail. Ever.
- OAuth providers (Microsoft 365 / Outlook.com no longer accept IMAP app passwords). Deferred to v2.
- Semantic / embedding search. Keyword search plus sender/date filters, with the AI choosing search terms, covers the target prompts. Revisit only if real usage shows gaps.
- Any network-listening transport. stdio only.
- Google contacts and calendar (OAuth). Writing to contacts or calendars, ever.

## 2. Origin

Started from a ~300-line prototype (`icloud-readonly-mcp`, Python, MCP SDK 2.x): single iCloud account, live IMAP search, EXAMINE + BODY.PEEK, link stripping, code masking, sender/keyword body withholding, CI grep guard. This plan generalizes it to multiple accounts, replaces live IMAP search with a local sanitized index, and fixes issues found in review (§10).

## 3. Threat model

**Primary attack chain:** the AI reads a reset email → opens the link (or uses the code) → takes over an account. The design breaks the chain in several independent places:

| Link in chain | Control |
|---|---|
| AI can send/modify mail | No SMTP code. IMAP layer exposes only narrow methods (§4.4) that build their own commands; nothing accepts a raw command or fetch expression. EXAMINE only (RFC 9051 §6.3.3 forbids permanent state changes). CI grep guard as a secondary tripwire. |
| AI sees reset link | Every URL stripped from every body (not only suspicious ones). HTML converted to text with `href`/`src` discarded. Bare domains (`evil.com/reset?t=…`) also stripped. |
| AI sees code / token | Digit runs, mixed alnum tokens (upper **and** lower case), long high-entropy strings masked. |
| AI sees security email | Body withheld (headers only) if sender domain blocklisted or sensitive keyword present. |
| Filter evasion | Unicode NFKC normalization; zero-width/format chars removed before matching; hidden HTML (`display:none`) dropped. |
| Search oracle (probe for code digit-by-digit) | Search runs against the **sanitized** index, where codes and withheld bodies were never stored. |
| AI opens a link anyway | Out of scope for this server; README advises not combining with browser/fetch tools. |
| Prompt injection via email content | Results labeled as untrusted third-party data. Cannot be fully prevented; mitigated by having no write/send/open capabilities. |
| Attachments as an exfil or exploit vector | Never opened without human approval via a channel the AI cannot answer (§7.1). Parsed in an isolated worker with no network, no credentials, and resource limits (§7.2). |

**Explicit residual risks (documented, not solved):**
- Content the AI reads is sent to the AI provider as part of the chat. Filters remove credentials, not privacy.
- Filters are heuristic: homoglyphs, codes spelled as words ("four eight two…").
- Approved images are returned as pixels and are not redacted. A code, QR code, or screenshot of a login page inside an approved image reaches the AI. The approval dialog says so for image items.
- The local index is a second (sanitized) copy of the user's mail on disk.
- Over-redaction: years, order numbers, phone fragments get masked. Deliberate.
- An assistant that also has desktop-automation tools (browser/computer use) could operate the approval dialog itself. Independent human approval holds only without such tools.
- "Never stored" for attachments means no intentional disk persistence by the application; OS swap and crash dumps are not controlled.
- A shell-capable AI agent running as the same OS user defeats every promise in §1 (see §3.1).

### 3.1 Credential isolation and the recommended setup

**Finding (live test, 2026-11-06).** `set-password` stores the app password in the OS keyring. On macOS the Keychain item's access list trusts the Python binary that created it, so any later process using that same Python reads it **with no prompt**. An AI coding agent with a shell, running as the same user, could therefore run one line of Python and obtain the app password, which grants full read/write/delete access to the mailbox at the provider and bypasses this server entirely. The same agent could also edit the server's code or `rules.yaml`, or read the index. This is not a bug in this project; it is how per-user credential stores work. **The OS user account is the security boundary.** Nothing inside a process can protect a secret from other code running as the same user.

**Recommended setup (best practice: least privilege via a separate OS user).** Choose by how you use AI tools:

| Your situation | Setup | Why |
|---|---|---|
| AI used only through chat clients whose only access to your machine is MCP tools (Claude Desktop, Cursor/VS Code chat with terminal and file tools off) | **Standard install** under your own user; password in the OS keyring. | The AI can call only this server's tools, which never return credentials. |
| Any shell- or file-capable AI agent also runs under your user on this machine | **Dedicated-user install (recommended).** Install and run the server as a separate, unprivileged, non-admin OS user (e.g. `mailmcp`). Its code, config, rules, index and password live in that user's home, unreadable and unwritable by your account. Your MCP client launches it through a single narrow rule, e.g. sudoers `you ALL=(mailmcp) NOPASSWD: /Users/mailmcp/.local/bin/readonly-safe-mail-mcp serve`. | Standard privilege separation, the same pattern used for system services. The agent can call the server's tools but cannot read the password, alter the rules, or replace the code. |
| You cannot create another OS user | Standard install, plus: keep agents out of the session where mail is configured, and **revoke the app password if an agent ever had unsupervised shell access.** | Documented residual risk, not a mitigation. |

Rejected as the primary control: setting the Keychain item to "confirm every access". It prompts on every sync, trains the user to click Allow, and does not stop an agent from editing the server's code or rules.

**Implementation consequences (scheduled in step 6):**
- Credential backend `file` for dedicated-user installs: one file per account in that user's config directory, mode `0600`, owned by that user; the server refuses to read it if the permissions are looser. (A login Keychain is not available to a user with no GUI session, so the keyring cannot serve this mode.)
- `check` warns when the server's code, config or credentials are writable or readable by any user other than the one running it.
- The README gives step-by-step dedicated-user setup for macOS and Linux, tested before release. Windows equivalent (separate local account) is documented once tested; until then Windows docs state the risk.
- `delete-password <account>` removes the local copy; revoking the app password at the provider is the only way to invalidate it everywhere. Both are documented next to `set-password`.
- `set-password` must be run in a real terminal; it refuses when stdin is not a TTY (e.g. an AI client's `!` shell) so a password can never be echoed into a transcript.

## 4. Architecture

```
 AI client (Claude / Cursor / VS Code)
        │ stdio (MCP)
        ▼
 readonly-safe-mail-mcp ──► SQLite index (sanitized text only, local disk)
        │
        └─(sync + approved attachments only)─► IMAP providers (read-only, allowlisted commands)
```

Queries **never** hit the network. IMAP is used only for (a) syncing new mail into the index and (b) fetching a single attachment after human approval.

### 4.1 Sanitizer pipeline (single code path for everything stored or returned)
Fixed order, so no step can introduce characters a later step would miss (e.g. `pass&#x200B;word` gains a zero-width character only at entity decoding):

1. MIME transfer-encoding and charset decoding
2. HTML → text, including entity decoding (drop script/style/head/hidden; discard all attributes)
3. Unicode NFKC normalization; remove zero-width and format characters
4. Security classification (sender domain, keywords) on the **full** decoded text of **every** body alternative (plain and HTML), before anything is discarded
5. Quoted-reply removal
6. URL and code/token redaction
7. Truncation

Applied to bodies, subjects, display names, attachment filenames, snippets, and extracted attachment text. Withhold reasons are generic ("account-security email") and never echo matched content. Any decode or parse failure withholds the affected content; raw content is never returned as a fallback.

### 4.2 Local index
- SQLite + FTS5 (BM25 ranking), WAL mode so reads proceed during sync writes.
- Stores per message: account, folder, UID, UIDVALIDITY, date, from, to, subject (sanitized), sanitized body (cap ~20 KB), withheld flag + reason, attachment metadata (sanitized filename, MIME type, size, part id).
- **Never stores:** raw messages, HTML, links, codes, withheld bodies, attachment contents.
- Location: `~/Library/Application Support/readonly-safe-mail-mcp/` (macOS) / platform equivalents. Directory and database, including `-wal` and `-shm` files, are owner-only (0700/0600; equivalent ACLs on Windows). Must not be placed in a synced folder (iCloud Drive, Dropbox, Desktop/Documents); server warns if it is.
- **Policy generations.** Each index generation records a fingerprint of the effective rules, sanitizer version, and extraction behavior. Every content-returning tool (search, snippets, read, passages, thread) checks that the generation matches the active policy before returning, and re-checks if the policy changed mid-operation. On mismatch the affected accounts return "index rebuilding under updated security rules" instead of content; unaffected accounts stay available. The rebuild writes a **new** generation (separate database file) and publishes it atomically on success. A failed or offline rebuild never reactivates the old generation. Cost: after a rule change, affected accounts are unsearchable until the rebuild completes (hours for large mailboxes). That is the price of stricter rules taking effect immediately.
- Estimated size: ~0.5–2 GB for ~100k messages (estimate; `check` reports a projection before first sync).
- Optional scope limits: per-account, per-folder, "last N years".

### 4.3 Sync policy (no timers, no background schedule)
Sync uses **zero AI tokens** (plain IMAP + Python), but runs only when triggered by a search:

| Condition at search time | Behavior |
|---|---|
| Last sync < 5 min (`sync_if_older_than`, configurable) | Search immediately. No sync. |
| 5–10 min | Search immediately; start sync in background (non-blocking). |
| > 10 min (2× interval) — "first search after a break" | Start sync, wait silently up to 2 s. If still running, send MCP progress notification: *"Syncing new mail (first search after a break; later searches are instant)…"*. At 10 s stop waiting: *"Continuing with unsynced data; newest mail will appear in your next search."* Sync continues in background. |
| Last **successful** sync > 10 min and sync failed/offline | Results prefixed with one-line staleness warning per account. |

Notes: accounts sync in parallel; at most one sync at a time; progress notifications render only in clients that support them (final result always carries the note). The server cannot detect "new chat," so "after a break" is defined by index age.

Initial sync for a large mailbox may take hours; run explicitly via `readonly-safe-mail-mcp sync` (shows progress). Incremental thereafter via UIDNEXT/UIDVALIDITY.

### 4.4 IMAP layer
- Narrow internal methods only: `fetch_headers`, `fetch_structure` (BODYSTRUCTURE), `fetch_text_part(part)`, `fetch_approved_attachment(approval)`. Each builds its own command; none accepts a raw command or fetch expression. Whole-message fetches (`BODY.PEEK[]`, `RFC822`) do not exist in the code.
- Sync fetches BODYSTRUCTURE first, then only `text/plain` and `text/html` body parts under a size cap. Attachment parts, inline attachments, and nested `message/rfc822` parts are indexed as metadata only and never fetched during sync.
- TLS: certificates verified against the **operating system's trust store** via `truststore` (macOS Keychain, Windows certificate store, Linux system bundle), TLS 1.2 minimum, hostname checking. Plain `ssl.create_default_context()` was rejected: python.org builds on macOS ship with no CA bundle, so every connection failed verification (found in the 2026-11-06 live test; it failed closed and no password was sent). No STARTTLS downgrade; credentials are never sent before verification succeeds. (Python's `imaplib.IMAP4_SSL` default context does **not** verify certificates; see §10.)
- Connection and per-command timeouts on every operation.
- Logs and exceptions never contain raw messages, attachment bytes, authentication exchanges, or redacted secrets.
- Limits enforced while fetching and decoding, not after: message size, MIME nesting depth, per-part bytes, concurrent requests. Advertised sizes are advisory; actual byte counts are enforced. A rejected item reports which limit it exceeded; other messages remain usable.
- Tests assert the exact command strings sent over a fake connection, including rejection of injected command fragments and invalid part identifiers.

## 5. Accounts and rules

`~/.config/readonly-safe-mail-mcp/accounts.yaml` (never committed):

```yaml
accounts:
  icloud: { preset: icloud, username: you@icloud.com }
  gmail:  { preset: gmail,  username: you@gmail.com }
  other:  { host: imap.example.com, port: 993, username: you@example.com }
folders: { allow: ["*"], deny: [Junk, Trash, "Deleted Messages", Drafts] }
sync_if_older_than: 5m
attachments: ask        # ask | never
# optional, per account: contacts: true, calendars: true (or { allow, deny }); top level: calendar_years: 10 (§14)
```

- Presets: iCloud, Gmail (app password + 2FA), Fastmail, Yahoo.
- Passwords via OS keyring (`keyring`: macOS Keychain / Windows Credential Manager / Linux Secret Service). `set-password <account>` CLI. Env-var fallback documented with plaintext warning.
- **Rule layering (fail-closed):** packaged generic defaults → user `rules.yaml` → per-account rules. Lists are **unioned**; user files can only add protection. Removing a default requires explicit `disable_default_keywords: [...]` and logs a warning. Unknown keys are a hard error.
- **No personal data in the repo:** only generic defaults ship. CI blocks committed `*.local.yaml`, non-example email addresses, and `accounts.yaml`.

## 6. MCP tools (kept few, with short descriptions, to save tokens)

| Tool | Purpose | Output (compact by design) |
|---|---|---|
| `list_accounts` | Accounts, last sync time, message counts | one line per account |
| `search` | FTS query (AND/OR/phrases) + filters: from, to, date range, account, folder; paging | one line per hit: id, account, date, from, subject, ~150-char snippet around match; default 20 |
| `find_passages` | Return only matching paragraphs within given message ids | paragraphs only, not whole emails |
| `read_message` | Full sanitized body of one message | quoted replies removed, capped |
| `get_thread` | Conversation around a message | de-duplicated, quotes removed |
| `request_attachments` | Ask human to approve opening 1+ attachments | see §7 |

Four more tools, `search_contacts`, `read_contact`, `search_events` and `read_event`, are registered only while an account has that source on (§14).

All tools: MCP `readOnlyHint=true`, `destructiveHint=false`. Results carry an "untrusted third-party content" marker.

Token-efficiency principles: snippets not bodies, paging not dumps, quote-stripping at index time, `find_passages` to avoid reading whole messages, minimal tool descriptions (they're sent every turn).

## 7. Attachments

Never downloaded or stored by default; only sanitized metadata (filename, type, size, part id) is indexed. Supported formats in v1: PDF, DOCX, XLSX, PPTX, TXT/CSV/TSV/Markdown, HTML, VCF (vCard), ICS (calendar invites), EML (attached emails), and images (JPEG, PNG, HEIC, GIF, WebP, TIFF, BMP). Archives, executables, disk images, and legacy binary Office files are refused. Per-format handling is in §7.3.

### 7.1 Approval
- `request_attachments([{message_id, part}...])` shows **the human** a batched table (account, date, from, subject, filename, size) with per-item checkboxes → Approve selected / Deny.
- **Default channel: a server-owned native dialog** (macOS `osascript` with values passed as arguments, never interpolated into script text; Windows and Linux equivalents). **MCP elicitation** is used only for clients the user lists as trusted in config, because an elicitation response is the client's report of a decision, not independent proof of one. If no channel is available: refuse.
- Each pending approval is bound to account, folder, UIDVALIDITY, UID, part id, and an expiring request id. Approval is consumed once. If the mailbox generation changed or the item no longer matches, the fetch is refused and fresh approval is required.
- Denial, cancellation, malformed responses, and timeouts never authorize a fetch.

### 7.2 Extraction worker
After approval the main server fetches that one part into memory and hands the bytes to a short-lived worker process that, before parsing, has:
- no network access and no inherited sockets or mail connections;
- no credentials in its environment and no access to the keyring or config directory;
- filesystem access limited to the interpreter/parser files and the supplied bytes;
- limits on memory, CPU time, pages, decompressed bytes, and output size, enforced during decoding, not after.

The worker exits after one request; timeout or cancellation kills it. The parent runs the extracted text through the sanitizer (§4.1) before returning it. Platform mechanisms: macOS `sandbox-exec` profile (built and tested, `isolate.py`); Linux `bwrap --unshare-all` when present (built, not yet tested on Linux); Windows: none yet, so attachments are refused there (restricted token + job object is future work). **Where the strong mechanism is unavailable, attachment opening is refused** (config `attachments: ask-unsandboxed` can override, with a warning). A timeout alone is never described as a sandbox. The package ships and tests these restrictions; users configure nothing.

### 7.3 Formats
All parsing and decoding happens inside the §7.2 worker.
- **PDF, DOCX, PPTX:** text extracted (PPTX per slide), then sanitized (§4.1).
- **ICS:** events rendered as text (title, times, location, organizer, attendees, description), then sanitized.
- **EML:** headers and body (plain preferred; HTML through the hidden-content stripper), nested attachments listed by name only, then sanitized.
- **XLSX:** cell values only (no formulas, macros, or external links), output as CSV-like text per sheet, then sanitized. Limits on sheets, rows, and cells.
- **TXT/CSV/HTML:** decoded, then sanitized (HTML goes through the same hidden-content stripping as bodies).
- **VCF:** fields rendered as text, then sanitized. Embedded `PHOTO` data is treated like an image.
- **Images:** decoded and re-encoded in the worker (pixels only). All metadata is dropped (EXIF, GPS location, XMP, embedded thumbnails), and the image is downscaled to a size cap. It is returned as image content, not text. The text sanitizer cannot redact pixels, so a code or QR code in an image reaches the AI after human approval (§3 residual risk).

## 8. Install & client support

README opens with install. Example (GitHub-pinned; PyPI form shown once published):

```sh
uvx --from git+https://github.com/pkellner/readonly-safe-mail-mcp@v0.3.1 readonly-safe-mail-mcp set-password icloud
uvx --from git+https://github.com/pkellner/readonly-safe-mail-mcp@v0.3.1 readonly-safe-mail-mcp check
uvx --from git+https://github.com/pkellner/readonly-safe-mail-mcp@v0.3.1 readonly-safe-mail-mcp sync
```

Copy-paste configs for: Claude Desktop (`claude_desktop_config.json`, `mcpServers`), Claude Code (`claude mcp add`), Cursor (`~/.cursor/mcp.json`), VS Code (`.vscode/mcp.json`, `servers`), generic stdio.

CLI subcommands: `set-password`, `delete-password`, `check` (test login, show one sanitized sample, project index size; a read-only login per contacts or calendar source that is on), `sync` (mail, then contacts and calendar), `status`, `print-rules` (effective merged rules).

## 9. Repo, CI, and review hygiene

- License: **MIT**. Fresh public history (prototype had none).
- `THREAT_MODEL.md` (expanded §3), `SECURITY.md` + GitHub private vulnerability reporting, `CONTRIBUTING.md`, "filter bypass" issue template (requires de-personalized sample).
- CI: actions pinned to commit SHAs; matrix Python 3.11–3.13 × Linux/macOS/Windows; `ruff`; CodeQL; Dependabot; IMAP-allowlist test; adversarial filter tests (zero-width, homoglyph, bare domains, lowercase tokens, hidden HTML, quoted-printable splits); personal-data guard.
- Branch protection on `main`.
- Acceptance tests required before the first public release: approved extraction of every supported format; denied/cancelled/expired/replayed/mismatched approvals; sync issues no whole-message or attachment fetches (asserted on the wire); worker attempts to reach network, credentials, and unrelated files are blocked; encoded keywords (`pass&#x200B;word`), malformed MIME, and sanitizer failures all withhold; policy change during search/read and failed rebuilds; invalid TLS certificate blocks login before credentials are sent; oversized and compressed inputs fail within limits.

## 10. Issues found in the prototype (fixed by this plan)

1. **Search oracle:** live IMAP `TEXT` search ran over raw bodies, letting a prompt-injected AI reconstruct a withheld code via ~60 substring searches. → Fixed by searching only the sanitized index.
2. `ALNUM_CODE_RE` matched uppercase only; lowercase reset tokens leaked. Bare domains without scheme/`www.` leaked.
3. No Unicode normalization; zero-width chars defeated keyword rules.
4. CI grep guard trivially bypassable (`getattr`). → Runtime command allowlist.
5. No IMAP timeouts; From/To display names unsanitized; hidden HTML text passed through.
6. Rules path only worked from a git checkout; password retrieval macOS-only.
7. **TLS not verified.** `imaplib.IMAP4_SSL(HOST, PORT)` with no `ssl_context` uses Python's unverified stdlib context (confirmed on Python 3.11.6: `ssl._create_stdlib_context is ssl._create_unverified_context`). The prototype would accept a forged certificate and send the app password to an on-path attacker. → Fixed by §4.4.

## 11. Decisions

- Name `readonly-safe-mail-mcp`; rename existing private repo `pkellner/icloud-readonly-safe-mcp` (GitHub redirects old URL).
- License MIT.
- Local sanitized index is the primary search path; no live IMAP search tool exposed.
- Search-triggered sync only (5 min interval; first search after a break waits silently 2 s, shows progress to 10 s, then continues with unsynced data). No timers.
- Attachments opened only with human approval.
- **PyPI:** upload a placeholder `0.0.1` early to reserve the name (README: "under review, not ready for use"). During review, real installs use GitHub-pinned tags; full PyPI releases begin at 1.0 via GitHub Actions trusted publishing. Note: PyPI is public and searchable; a placeholder is the only form of name reservation.

## 12. Review log and open questions

**Round 1 (external design review): all nine recommendations adopted.** Guarantee wording (§1), narrow IMAP methods (§4.4), concrete worker isolation (§7.2), bound single-use approvals with native dialog default (§7.1), decode-before-normalize pipeline (§4.1), policy generations (§4.2), TLS/storage/logging defaults (§4.2, §4.4), resource bounds (§4.4, §7.2), acceptance tests (§9). One qualification: where a platform lacks a strong isolation mechanism, attachment opening fails closed rather than shipping a weaker "sandbox".

**Live test against iCloud (2026-11-06):** read-only `check` passed (TLS verified, LOGIN, LIST, EXAMINE, one partial header fetch; iCloud accepts `HEADER.FIELDS` with `<0.N>`). Findings: (a) default TLS context had no CA bundle on python.org macOS builds; fixed with `truststore` (§4.4). (b) Keychain items are readable without a prompt by any process using the same Python, so shell-capable agents can obtain the password; recommended setup added (§3.1). (c) `set-password` crashed without a TTY; now refuses cleanly.

**Still open for reviewers:**
1. Can you get a reset link, code, or token past the sanitizer? Test cases welcome.
2. Should the index be encrypted (SQLCipher) beyond full-disk encryption?
3. Is fail-closed on unsandboxed platforms (§7.2) the right call, or should Windows get a weaker-but-present worker?
4. Any way a user config could weaken defaults silently (§5)?
5. Is the dedicated-user install (§3.1) the right default recommendation, and is the narrow sudoers rule the cleanest way for an MCP client to launch it?

## 13. Build order

1. ✅ Rename repo; restructure into package (accounts, rules layering, keyring).
2. ✅ Read-only IMAP layer (§4.4: narrow methods, verified TLS, limits); sanitizer pipeline (§4.1) + adversarial tests.
3. ✅ Sanitized SQLite/FTS5 index with policy generations; initial + incremental sync (parallel fetch over 2 connections plus a control connection, resumable); search-triggered sync policy.
4. ✅ MCP tools (§6) with compact outputs.
5. ✅ Attachment approval + isolated extraction worker (§7): macOS done and live-tested; Linux `bwrap` untested; Windows refused.
6. ✅ README (install first), THREAT_MODEL.md, SECURITY.md, CONTRIBUTING.md, filter-bypass issue template; CI (SHA-pinned, 3 OS × 3 Python), CodeQL, Dependabot, personal-data guard; `file` credential backend and permission warnings in `check` (§3.1). CI first runs on push.
7. ◐ Owner runs `check` + first sync on real accounts (iCloud done 2026-11-06; dedicated-user setup steps not yet tested).
8. Repo goes public; branch protection on `main`; PyPI per open decision.
9. ✅ Contacts and calendars (§14, 0.3.0), built against a simulated server; a live read-only check against iCloud is not done yet.

## 14. Contacts and calendars (0.3.0)

The assistant can also search the user's contacts and calendars over CardDAV and CalDAV, read-only, with the password already stored for mail. Both are off until an account sets `contacts: true` or `calendars: true` (iCloud, Fastmail, Yahoo; Gmail is a config error). With both off, the tool list, `instructions`, `list_accounts`, `status` and the mail index file are unchanged (`tests/test_mail_untouched.py`).

- **Promise (structural, like promise 1 in §1):** no operation writes to contacts or calendars. The only HTTP client is `GuardedDAVConnection` (`dav.py`), which refuses anything but `PROPFIND` and `REPORT`, a foreign host, a body this package did not build, or a header outside the fixed set, inside the connection object, before a byte is sent. The password allows writes at the provider, so this guard is the only barrier (THREAT_MODEL.md).
- **Parser and field policy.** `vcal.py` parses vCard and iCalendar into raw values (shared with the attachment renderer, which imports its line helpers). `fieldpolicy.py` decodes, normalizes, classifies, redacts and caps every field, with its own version (`FIELD_POLICY_VERSION`); the mail `SANITIZER_VERSION` is untouched. Phones and postal addresses are shown in validated shapes (the exception to §3's over-redaction); notes and descriptions get the full mail redaction; websites, photos, messaging handles, alarms, attachments and conference links are dropped.
- **Second index.** `dav-<fingerprint>.sqlite` beside the mail index (`davindex.py`), same modes and generations, its own schema version. Collections, cards, events with validated timing and an incomplete reason, occurrences, per-source status and the window indexed. Phones are also stored digits-only.
- **Sync (`davsync.py`).** After the mail sync has released its lock, under its own lock, on its own thread when started by a tool: a DAV failure never reaches the mail sync. Collections are discovered; rows are deleted, and the ctag stored, only after a complete, validated listing and every changed item processed (a listing whose own entry carries an error status, such as 507 for a truncated answer, is not complete; a known collection whose type did not come back makes discovery incomplete; a collection now served from another partition host keeps its rows). Anything less deletes nothing and the next sync retries. A multiget over the size cap is halved; one item over the cap alone is stored unreadable. Tools use mail's policy of §4.3 (start a due sync; the first call after a break waits up to 10 s), with a note when the answer comes from data older than the interval or from an unfinished first sync; after a failed attempt the next waits a full interval from that attempt, so a wrong password is not retried at every call.
- **Calendar (`caltime.py`, `expand.py`).** Times with `Z` or an IANA `TZID` are exact; all-day values are dates; floating time is read in the machine's zone and labelled; a `TZID` that is not an IANA name is read as floating and marked incomplete (this covers Outlook's Windows names). `RRULE` with FREQ daily or coarser is expanded in a worker child process on the runner of §7.2 (sandboxed where it can sandbox, a plain child elsewhere: with the CPU limit on other POSIX systems, with the wall-clock kill only on Windows), in batches with deadlines and a 60 s budget per sync; the rest is pending until the next sync. The caps on the values do not bound the work alone (a rule that never matches makes dateutil search to the year 9999): a one-second limit per event inside the worker (POSIX, SIGALRM) and the kill do. Deadlines count from the worker's measured start; a worker that gives no usable answer marks no event and stops the pass, with the error shown per source. `caltime.TIMING_VERSION` is in the second file's fingerprint, so a fix to how timing is read reaches every stored event. What cannot be understood is kept, marked incomplete, and named in every date search it could touch, so a search never looks empty by mistake.
- **Window.** `calendar_years` (top level, 1 to 50, default 10): years before and after the current month. Occurrences are rebuilt locally, with no request, when the window, the option or the expander version changes. The option is shown in the installer's `accounts.yaml` line and question, in `status` and `list_accounts` beside the range indexed, in the reply to a search outside the range, and in the README.
- **Not done:** Google; reading the Mac's own Contacts and Calendar stores; to-dos, journals and photos; embedded `VTIMEZONE` definitions and sub-daily rules (marked incomplete); a live check against iCloud.
