# readonly-safe-mail-mcp

Let an AI assistant search and read **all your email accounts** (iCloud, Gmail, Fastmail,
Yahoo, any IMAP server) without giving it the power to send, change, or delete mail, and
without handing it the password-reset links and login codes that sit in every inbox.
Optionally it can also search your **contacts and calendar** (iCloud, Fastmail, Yahoo),
read-only, with the same app-specific password. See [Contacts and calendars](#contacts-and-calendars-optional).

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
> Linux, and Windows are untested. Contacts and calendars have so far been tested only
> against a simulated server, not against a real account.

## Quick install (Mac, one iCloud account)

Open **Terminal** (Applications → Utilities) and paste:

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/pkellner/readonly-safe-mail-mcp/main/install.sh)"
```

It asks for your iCloud address, asks whether Claude may also search your iCloud contacts
and calendar (both stay off unless you say yes), opens Apple's page so you can make an
app-specific password, and does the rest: installs Python and the tool, stores the password in your
Keychain, tests a read-only login, connects Claude Code, Claude Desktop and Codex
(whichever you have), and starts the first sync. The connection covers every session
of those apps on this Mac; ones that were already open need a restart to see it. You don't need Python, Homebrew, git, or Xcode, and it
doesn't touch the Mac's system files or your shell settings.

**Made a mistake, or want to change something?** Paste the same command again. It shows
your current setup and asks what you want: keep it (re-test and reconnect), change the
email address, enter a new password, turn contacts and calendar on or off, or start over
from scratch.

**Update to a new version.** Paste the same command again. Whichever choice you make, it
installs the latest release and keeps your account, password and downloaded mail; choose 1
to keep everything as it is. If you have not been asked about contacts and calendar yet, it
asks now, yes or no for each; choose 6 on a later run to change the answers. If your
settings file holds more than the installer writes (several accounts, folder rules, your own
comments), it leaves the file alone and shows the two lines to add yourself (see
[Contacts and calendars](#contacts-and-calendars-optional)). It then re-tests the login. Restart open Claude sessions to see new tools. If the test says
`Keychain Access Denied`, run `~/.local/bin/readonly-safe-mail-mcp set-password icloud` once
in Terminal.

**Uninstall.** Paste this into Terminal:

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/pkellner/readonly-safe-mail-mcp/main/uninstall.sh)"
```

It lists what it found and asks once, then removes all of it: the tool, its settings, the
saved password in your Keychain, all downloaded data (mail, and contacts and calendar if you
turned them on), and the connections in Claude Code,
Claude Desktop and Codex (other servers there are left alone). It asks separately before
removing uv. Your mail, contacts and calendar on iCloud are never touched. Afterwards, revoke the app-specific
password at [account.apple.com](https://account.apple.com) → Sign-In and Security →
App-Specific Passwords.

Then ask Claude about your email. Want to see what it does first? Read
[install.sh](install.sh). Everything below is optional detail.

> **Status: pre-release (0.3).** Read [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md)
> before trusting it with your mail.

## Install

Requires [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
uv fetches Python 3.11+ itself if you don't have it, and git isn't needed.

```sh
uv tool install --python 3.13 https://github.com/pkellner/readonly-safe-mail-mcp/archive/refs/tags/v0.3.0.tar.gz
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

To also search contacts and calendar, add `contacts: true` and `calendars: true` to an
iCloud, Fastmail or Yahoo account; see [Contacts and calendars](#contacts-and-calendars-optional).

### 3. Store the password, check, and sync

```sh
readonly-safe-mail-mcp set-password icloud   # run in a real terminal; stored in the OS keyring
readonly-safe-mail-mcp check                 # verified TLS login, one sanitized sample, sandbox status
                                             # (and a read-only login per contacts or calendar source, if on)
readonly-safe-mail-mcp sync                  # first sync: newest mail first, safe to stop and resume
```

The first sync of a large mailbox takes a while (expect a few hours for 100,000+
messages with iCloud). Recent mail is searchable within minutes. After that, sync runs by
itself in the background when you search, and only fetches what's new.

Sync opens at most three connections to the mail server at a time: two that download in
parallel and one that steps through folders. That keeps it well inside provider limits.
If iCloud is pushed too hard it can refuse logins with
`[UNAVAILABLE] Service temporarily unavailable` for a day or more. That is not a password
problem. Run `check` to test the login at any time; if it shows that error, wait and try
again later. Syncing resumes where it left off once iCloud accepts logins again.

### 4. Connect your AI client

**Codex (OpenAI)**: the Quick install connects Codex by itself when it finds it. By hand:

```sh
codex mcp add readonly_safe_mail -- "$HOME/.local/bin/readonly-safe-mail-mcp" serve
```

Codex keeps this in its global settings, so it covers every Codex session. Quit Codex
and open it again, then start a new task. See [Install with Codex on a Mac](README-CODEX.md)
for a step-by-step iCloud setup guide and a ready-to-send email for a friend, including
Apple app-specific password instructions.

**Claude Code**

```sh
claude mcp add --scope user readonly-safe-mail -- readonly-safe-mail-mcp serve
```

- `--scope user` makes the server available to every Claude Code session on this
  computer. Without it the server is registered for the current project only, and
  sessions in other folders cannot search mail. Check with `claude mcp get readonly-safe-mail`.
- Sessions that are already running pick it up after a restart: type `/exit`, then run
  `claude --continue` in the same folder to get the conversation back. Other computers
  need their own install and their own `claude mcp add`.
- If `readonly-safe-mail-mcp` is not on your `PATH`, give the full path, for example
  `~/.local/bin/readonly-safe-mail-mcp`.
- With `--scope user`, every session in every folder can search your mail, including
  sessions that read untrusted material. The server is read-only, but decide whether
  you want that.
- **Register the same copy that stored the password.** On a Mac the Keychain lets only
  the Python that saved the password read it back. If you have two copies (say the
  installed one and a development checkout) and register the other one, `check` reports
  `password MISSING ... Keychain Access Denied` and syncing stops. Fix it by running
  `set-password ACCOUNT` with the copy you registered, in Terminal.
- A development checkout runs whatever code is in its folder at that moment. Register
  one for all sessions only if you want every session to run your work in progress.

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
*"what did the school say about the field trip?"* With contacts and calendar on: *"what is the
plumber's phone number?"* or *"what do I have on in the week of 3 March?"*

## Tools the assistant gets

| Tool | What it does |
|---|---|
| `list_accounts` | Accounts, sync status, message counts |
| `search` | Full-text search with filters (from, to, dates, account, folder), one line per hit |
| `find_passages` | Only the paragraphs that mention a query, from chosen messages |
| `read_message` | One message, sanitized, quoted replies removed; lists attachments with part numbers |
| `get_thread` | The whole conversation, oldest first |
| `request_attachments` | Asks **you** to approve opening attachments; returns their redacted text or images |

Four more tools exist only while an account has the matching source turned on
([below](#contacts-and-calendars-optional)):

| Tool | What it does |
|---|---|
| `search_contacts` | Words, name, organisation, email or phone (digits in any format); one line per card |
| `read_contact` | One card: phones, emails, addresses, birthday, note |
| `search_events` | Calendar events by date range and/or words; one line per occurrence |
| `read_event` | One event: time, repeat rule, location, people, description |

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

## Contacts and calendars (optional)

Off by default. With it on, the assistant can answer "what is the plumber's number?" or
"what is on in the week of 3 March?" from a local, sanitized copy, as for mail.

**Turn it on.** The installer asks. By hand, per account (iCloud, Fastmail and Yahoo; the
same app-specific password is used; `check` tests the login):

```yaml
accounts:
  icloud: { preset: icloud, username: you@icloud.com, contacts: true, calendars: true }
  # only some calendars, by name (default: all; case and spacing ignored; `check` lists
  # names that match no calendar):
  # icloud: { ..., calendars: { allow: [Home, Work], deny: [Birthdays] } }
calendar_years: 10    # years of calendar indexed before and after this month (1 to 50)
```

Gmail is not supported (Google needs OAuth, which this server does not do); turning either
source on for it is a config error. Both are read-only protocols (CardDAV and CalDAV). The
first sync runs after the mail sync, and after that on the same schedule.

**Read-only, and how that is enforced.** Your app-specific password allows writes at the
provider; this server never uses that. There is no write code, and the only HTTP client in
the package is a connection class (`dav.py`) that checks every request itself, before a byte
is sent: only `PROPFIND` and `REPORT` (read-only by specification), only the provider's own
hosts, only request bodies this package builds, and a fixed set of headers. `PUT`, `DELETE`,
`PROPPATCH`, `POST`, `MOVE`, `COPY`, `MKCOL`, `LOCK`, `GET` and the rest are refused, as is a
redirect to another host, so the password goes nowhere else. Tests record what a fake
server receives and assert that none of them arrives.

**What the assistant sees.**
- **Contacts:** names, organisations, emails, birthdays, and phone numbers and postal
  addresses. Phones and addresses are shown on purpose, the one place the usual redaction
  of digits is switched off: a contact is only useful with them. A number in a phone field,
  or in an address, is therefore shown even if it is really a code (see the
  [threat model](docs/THREAT_MODEL.md#contacts-and-calendars) for the exact shapes kept). They still go to your AI
  provider, so turn contacts on only if you accept that. Notes get the full mail redaction
  (links, codes, security keywords). Websites, photos and messaging handles are not shown.
- **Calendar:** title, time, place, people (at most 30) and description, redacted the same
  way; a description is withheld if it looks security-related. Alarms, attachments and
  conference links are not shown. Cancelled events and invitations you have not accepted
  are kept and labelled; an invitation in which your address is not found is labelled
  "(invitation, acceptance unknown)".
- Calendar text is written by strangers (anyone can send an invitation), so every result
  is labelled untrusted content, and repeat rules are expanded in a separate limited
  process, not in the server (see the [threat model](docs/THREAT_MODEL.md#contacts-and-calendars)).

**How far the calendar reaches.** `calendar_years` (default 10) sets how many years before
and after the current month are indexed. It is shown in `accounts.yaml` (the installer
writes it as a comment), in `status` and `list_accounts` next to the range actually indexed,
and in the reply to an event search that goes outside the range, which names it. Raise it
to search further; nothing is downloaded again, the dates are rebuilt locally at the next
sync.

**Limits you may notice.**
- **Incomplete events.** An event the server cannot fully place in time is kept but marked
  incomplete, and every date search it could touch ends with a line naming it and the reason,
  so a date search never looks empty by mistake. Reasons: a time zone it does not know, a
  repeat rule finer than daily or using hours or minutes, `EXRULE`, more than one rule, a
  start before 1900, and a few other rare forms.
- **Outlook invitations** often carry Windows time-zone names ("Pacific Standard Time"), not
  standard ones. Those events are read as floating times (your Mac's own zone), labelled
  "(floating)" and marked incomplete.
- **On Windows**, your machine's own time zone is read with today's daylight-saving rule for
  every year, so floating and all-day times in a year whose rule differed can be an hour
  off near the change. Repeat rules there are bounded only by the time limit on the whole
  expansion process, not per event.
- **A failing contacts or calendar sync** (a wrong password, say) is tried again at most
  once per sync interval, so the provider does not see a login attempt at every question.
  Answers from data older than the interval say so.
- **Repeating events** are expanded in batches under a time budget. After a large change some
  may wait for the next sync; `status` says how many ("pending expansion").
- An event's id changes when the event changes on the server.
- "Invitation, not accepted" is matched against your account's username only, not your other
  addresses: an invitation sent to an alias reads "(invitation, acceptance unknown)". So does
  an event in a calendar someone shares with you, when it has attendees and you are not one.
- Reminders (to-dos), journals and contact photos are not read. Only iCloud is the intended
  target; Fastmail and Yahoo use the same protocols, with hosts built in, and are unchecked.

## Configuration

`accounts.yaml` top-level options:

```yaml
# Folders the server flags as Junk, Trash or Drafts are always skipped. A `deny` list
# replaces the default name list (Junk, Spam, Trash, Deleted Messages, Deleted Items, Drafts).
folders: { allow: ["*"], deny: [Junk, Spam, Trash, "Deleted Messages", Drafts, Ads] }
sync_if_older_than: 5m       # background sync when the index is older than this
attachments: ask             # ask | never | ask-unsandboxed
trusted_elicitation_clients: []  # clients allowed to show the approval prompt themselves
calendar_years: 10           # contacts and calendar only: years of calendar before and after this month, 1 to 50
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
| `set-password ACCOUNT [--store file]` | Store a password in the OS keyring (or a mode-600 file, for dedicated-user installs). On a Mac it offers to use the clipboard, and always asks first |
| `delete-password ACCOUNT` | Remove the stored password (also revoke it at the provider) |
| `check` | Config, permissions, sandbox, and a read-only test login for mail, and for contacts and calendar where on (no sync) |
| `sync [--account A]` | Sync now with progress: mail first, then contacts and calendar (Ctrl-C to stop; resumes later) |
| `status` | Offline: what's indexed and when it last synced, per source; with calendar on, the range indexed and `calendar_years` |
| `print-rules`, `paths` | Show effective rules and where files live |

### Shortcuts (optional, Mac)

The Quick install does not put the program on your `PATH`, so the commands need the full
path, for example `~/.local/bin/readonly-safe-mail-mcp status`. To type `mail-status`
instead, copy all of the lines below, paste them into Terminal, press Return, then open a
new Terminal window. They add shortcuts to your `~/.zshrc` file. The installer prints the
same block at the end, with the exact path for your Mac.

```sh
cat >> ~/.zshrc <<'MAIL_SHORTCUTS'

# readonly-safe-mail-mcp shortcuts
alias mail-status='"$HOME/.local/bin/readonly-safe-mail-mcp" status'
alias mail-check='"$HOME/.local/bin/readonly-safe-mail-mcp" check'
alias mail-sync='"$HOME/.local/bin/readonly-safe-mail-mcp" sync'
alias mail-rules='"$HOME/.local/bin/readonly-safe-mail-mcp" print-rules'
alias mail-paths='"$HOME/.local/bin/readonly-safe-mail-mcp" paths'
alias mail-set-password='"$HOME/.local/bin/readonly-safe-mail-mcp" set-password icloud'
alias mail-delete-password='"$HOME/.local/bin/readonly-safe-mail-mcp" delete-password icloud'
MAIL_SHORTCUTS
```

| Shortcut | Does |
|---|---|
| `mail-status` | Show download progress and what is indexed (offline) |
| `mail-check` | Test the settings, the saved password and a read-only connection |
| `mail-sync` | Download mail now |
| `mail-rules` | Advanced:  show the rules in effect |
| `mail-paths` | Advanced:  show where files are kept |
| `mail-set-password` | Advanced:  replace the saved password |
| `mail-delete-password` | Advanced:  remove the saved password |

If your account is not named `icloud`, change that word in the last two lines. Pasting the
block twice is harmless; it only repeats the lines in `~/.zshrc`.

## Where your data lives

- Index: `~/Library/Application Support/readonly-safe-mail-mcp/` (macOS),
  `~/.local/share/readonly-safe-mail-mcp/` (Linux), `%LOCALAPPDATA%` (Windows). It is
  owner-only and holds sanitized text only: no raw mail, links, codes, withheld bodies, or
  attachment contents. Roughly 4–5 KB per message. With contacts or calendar on, a second
  file beside it (`dav-*.sqlite`) holds those, sanitized the same way except that phone
  numbers and postal addresses are kept.
- Keep it out of iCloud Drive, Dropbox, Desktop, and Documents. `sync` refuses folders that
  look cloud-synced.
- To delete only the downloaded data, remove that folder; the next sync rebuilds it. To
  remove everything, run the uninstall command under [Quick install](#quick-install-mac-one-icloud-account).

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
