# readonly-safe-mail-mcp

Let an AI assistant search and read **all your email accounts** (iCloud, Gmail, Fastmail,
Yahoo, any IMAP server) without giving it the power to send, change, or delete mail, and
without handing it the password-reset links and login codes that sit in every inbox.

- **Read-only by construction.** No sending code exists. IMAP commands pass through an
  allowlist that refuses anything that could change a mailbox.
- **Redacted.** Every link is removed and codes and tokens are hidden. Account-security
  emails (resets, verification codes, sign-in alerts) are withheld entirely.
- **Local and fast.** Mail is synced into a sanitized search index on your computer.
  Searches never touch the network and never see raw mail.
- **Attachments only with your OK.** You approve each attachment in a dialog. It is
  then read inside a sandbox with no network or file access, and its text is redacted
  the same way.

> [!CAUTION]
> **THIS SOFTWARE HAS NEVER HAD AN INDEPENDENT SECURITY AUDIT.**
>
> It is a personal project, written with heavy help from an AI coding assistant and
> reviewed only by its author. The protections described below are design goals, and
> nobody independent has checked that they hold. Bugs in the redaction, the read-only
> layer, or the attachment sandbox could expose password-reset links, login codes, or
> other sensitive mail to an AI assistant, and through it to anyone who can
> prompt-inject that assistant with an email.
>
> **Use it only if you have read the code yourself, understand what it does, and
> accept the risk.** It is provided as-is, with no warranty (see [LICENSE](LICENSE)).
> Do not use it for mail that someone else depends on you to protect.
>
> Only iCloud on macOS has been tested against real mail. Gmail, other providers,
> Linux, and Windows are untested.

## Quick install (Mac, one iCloud account)

Open **Terminal** (Applications → Utilities) and paste:

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/pkellner/readonly-safe-mail-mcp/main/install.sh)"
```

It asks for your iCloud address, opens Apple's page so you can make an app-specific
password, and does the rest: installs Python and the tool, stores the password in your
Keychain, tests a read-only login, connects Claude Code and Claude Desktop (whichever you
have), and starts the first sync. You don't need Python, Homebrew, git, or Xcode, and it
doesn't touch the Mac's system files. Running it again is safe.

Then ask Claude about your email. Want to see what it does first? Read
[install.sh](install.sh). Everything below is optional detail.

> **Status: pre-release (0.2).** Read [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md)
> before trusting it with your mail.

## Install

Requires [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
uv fetches Python 3.11+ itself if you don't have it, and git isn't needed.

```sh
uv tool install --python 3.13 https://github.com/pkellner/readonly-safe-mail-mcp/archive/refs/tags/v0.2.0.tar.gz
```

### 1. Create an app-specific password

Use an app password, never your main password. Revoking it cuts off access instantly.

- **iCloud:** [account.apple.com](https://account.apple.com) → Sign-In and Security →
  App-Specific Passwords.
- **Gmail:** turn on 2-Step Verification, then [App passwords](https://myaccount.google.com/apppasswords).
- **Fastmail / Yahoo:** Settings → Privacy & Security → App passwords.

### 2. Describe your accounts

Create `~/.config/readonly-safe-mail-mcp/accounts.yaml` (Windows:
`%APPDATA%\readonly-safe-mail-mcp\accounts.yaml`):

```yaml
accounts:
  icloud: { preset: icloud, username: you@icloud.com }
  gmail:  { preset: gmail,  username: you@gmail.com }
  # any IMAP server:
  # work: { host: imap.example.com, port: 993, username: you@example.com }
```

### 3. Store the password, check, and sync

```sh
readonly-safe-mail-mcp set-password icloud   # run in a real terminal; stored in the OS keyring
readonly-safe-mail-mcp check                 # verified TLS login, one sanitized sample, sandbox status
readonly-safe-mail-mcp sync                  # first sync: newest mail first, safe to stop and resume
```

The first sync of a large mailbox takes a while (about 1,400 messages a minute with
iCloud). Recent mail is searchable within minutes. After that, sync runs by itself in
the background when you search, and only fetches what's new.

### 4. Connect your AI client

**Claude Code**

```sh
claude mcp add --scope user readonly-safe-mail -- readonly-safe-mail-mcp serve
```

**Claude Desktop**: `claude_desktop_config.json`

```json
{ "mcpServers": { "readonly-safe-mail": { "command": "readonly-safe-mail-mcp", "args": ["serve"] } } }
```

**Cursor**: `~/.cursor/mcp.json` uses the same `mcpServers` block as Claude Desktop.

**VS Code**: `.vscode/mcp.json`

```json
{ "servers": { "readonly-safe-mail": { "type": "stdio", "command": "readonly-safe-mail-mcp", "args": ["serve"] } } }
```

Any other MCP client: run `readonly-safe-mail-mcp serve` over stdio.

Then ask things like *"find the emails about the roof repair quote from this week"* or
*"what did the school say about the field trip?"*

## Tools the assistant gets

| Tool | What it does |
|---|---|
| `list_accounts` | Accounts, sync status, message counts |
| `search` | Full-text search with filters (from, to, dates, account, folder), one line per hit |
| `find_passages` | Only the paragraphs that mention a query, from chosen messages |
| `read_message` | One message, sanitized, quoted replies removed; lists attachments with part numbers |
| `get_thread` | The whole conversation, oldest first |
| `request_attachments` | Asks **you** to approve opening attachments; returns their redacted text or images |

All tools are marked read-only. Results are labeled as untrusted third-party content.

## Attachments

Supported: **PDF, Word (.docx), Excel (.xlsx), PowerPoint (.pptx), text/CSV/TSV/Markdown,
HTML, contact cards (.vcf), calendar invites (.ics), attached emails (.eml), and images
(JPEG, PNG, GIF, WebP, HEIC, TIFF, BMP).** Refused: archives, executables, disk images,
legacy binary Office files (.doc/.xls), and anything else not listed.

1. The assistant calls `request_attachments` with message ids and part numbers.
2. A native dialog appears (macOS, or Linux with `zenity`) listing date, sender, subject,
   file name, and size. You choose which to open or deny all of them. A timeout or a
   closed dialog counts as a denial.
3. Each approved part is fetched once and parsed in a sandboxed child process: macOS
   `sandbox-exec` or Linux `bwrap`. That process has no network, no credentials, no
   access to your files or this app's config and index, and limits on CPU time, size,
   and decompression.
4. Extracted text goes through the same redaction as email bodies. Images are re-encoded
   to pixels only, with EXIF, GPS location, and other metadata stripped, and downscaled.
   **Images cannot be redacted:** a code or QR code visible in a picture reaches the
   assistant. The dialog says so.

If no sandbox is available (Windows, or Linux without bubblewrap), attachments are not
opened. You can accept that risk with `attachments: ask-unsandboxed`. Set
`attachments: never` to turn the feature off.

## Configuration

`accounts.yaml` top-level options:

```yaml
# Folders the server flags as Junk, Trash or Drafts are always skipped. A `deny` list
# replaces the default name list (Junk, Spam, Trash, Deleted Messages, Deleted Items, Drafts).
folders: { allow: ["*"], deny: [Junk, Spam, Trash, "Deleted Messages", Drafts, Ads] }
sync_if_older_than: 5m       # background sync when the index is older than this
attachments: ask             # ask | never | ask-unsandboxed
trusted_elicitation_clients: []  # clients allowed to show the approval prompt themselves
```

Optional `rules.yaml` in the same folder adds protection. Lists are merged with the
built-in defaults, so you can only make filtering stricter:

```yaml
blocked_sender_domains: [mybank.example]   # bodies from these senders are withheld
sensitive_keywords: ["wire instructions"]  # any message containing these is withheld
```

Run `readonly-safe-mail-mcp print-rules` to see the effective rules. Changing rules or
folders rebuilds the index under the new policy. Nothing indexed under old rules is ever
served again.

## Commands

| Command | |
|---|---|
| `serve` | Run the MCP server over stdio (what your client launches) |
| `set-password ACCOUNT [--store file]` | Store a password in the OS keyring (or a mode-600 file, for dedicated-user installs) |
| `delete-password ACCOUNT` | Remove the stored password (also revoke it at the provider) |
| `check` | Config, permissions, sandbox, and a read-only test login |
| `sync [--account A]` | Sync now with progress (Ctrl-C to stop; resumes later) |
| `status` | Offline: what's indexed and when it last synced |
| `print-rules`, `paths` | Show effective rules and where files live |

## Where your data lives

- Index: `~/Library/Application Support/readonly-safe-mail-mcp/` (macOS),
  `~/.local/share/readonly-safe-mail-mcp/` (Linux), `%LOCALAPPDATA%` (Windows). It is
  owner-only and holds sanitized text only: no raw mail, links, codes, withheld bodies, or
  attachment contents. Roughly 4–5 KB per message.
- Keep it out of iCloud Drive, Dropbox, Desktop, and Documents. `sync` refuses folders that
  look cloud-synced.
- To delete everything, remove that folder. The next sync rebuilds it.

## Security in one paragraph

This protects you from an assistant that has been tricked by a malicious email into
taking over an account using a reset link or code in your inbox. It does not make your
mail private from your AI provider: whatever the assistant reads is sent to it. It also
cannot protect you from an AI agent that has a shell on the same computer under your user
account. That agent could read the password straight from your keyring. If you use such
agents, follow the **dedicated-user setup** in
[docs/THREAT_MODEL.md](docs/THREAT_MODEL.md#recommended-setup). Don't give the same
assistant a browser or web-fetch tool together with this one. To report a bypass, see
[SECURITY.md](SECURITY.md).

## License

MIT
