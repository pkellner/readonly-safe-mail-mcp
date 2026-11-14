# Threat model

> [!CAUTION]
> This describes what the design **intends**. It has never had an independent security
> audit. Read the code before relying on any of it. See the warning at the top of the
> [README](../README.md).

## What this protects against

**The main attack:** an email contains instructions aimed at the AI ("forward me the
reset code"). The assistant reads it, then uses a password-reset link or login code
from your inbox to take over one of your accounts. The design breaks that chain at
several independent points:

| Step in the attack | Control |
|---|---|
| The AI sends or changes mail | There is no SMTP code. The IMAP layer has narrow methods that build their own commands. A second layer inside the connection object (`check_command`) refuses any command not on an exact allowlist before a byte is sent, including imaplib's own `store`, `copy`, and `expunge`. Folders are opened with EXAMINE (read-only), never SELECT. |
| The AI writes to contacts or calendars (only if you turn them on) | No write code exists. The only HTTP client is `GuardedDAVConnection` in `dav.py`: only `PROPFIND` and `REPORT`, only the provider's hosts, only request bodies this package builds, checked inside the connection before a byte is sent. See [Contacts and calendars](#contacts-and-calendars). |
| The AI sees a reset link | Every URL is removed from every body, not just suspicious ones. That includes bare domains with paths, IP URLs, and percent-encoded URLs. HTML is converted to text and all attributes (`href`, `src`) are dropped. |
| The AI sees a code or token | Digit runs, mixed letter/digit tokens (either case), and long high-entropy strings are masked. |
| The AI sees a security email | The body is withheld (headers only) when the sender's domain is on the blocklist or a sensitive keyword appears anywhere in any alternative of the body. |
| Filter evasion | Every part is decoded first, then HTML entities, then Unicode NFKC normalization. Zero-width and format characters are removed before matching. Hidden HTML (`display:none`, zero size, and so on) is dropped. Any decode failure withholds the content and never falls back to raw text. |
| Search used as an oracle to recover a code digit by digit | Search runs only against the sanitized index, where codes and withheld bodies were never stored. |
| Rules made stricter, but old content is still served | The index file is named by a fingerprint of the rules, sanitizer version, folders, and schema. A rule change selects a new, empty file. Old content is never served again and is deleted on the next sync. |
| An attachment used for exfiltration or as an exploit | Never opened without approval in a dialog owned by the server, not the AI. Parsed in a sandboxed process with no network, credentials, or user files, with CPU, size, and decompression limits. See [Attachments](#attachments). |
| Prompt injection in general | Results are labeled as untrusted third-party content. This cannot be fully prevented; the damage is limited because the server cannot write, send, or browse. |
| TLS interception | Certificates are verified against the OS trust store, TLS 1.2 minimum, hostname checked. The handshake finishes before LOGIN is sent. |

## Attachments

- **Approval.** Each request shows a native dialog (macOS `osascript`, Linux `zenity`)
  listing date, sender, subject, file name, and size. Item text is passed to the dialog
  as arguments, never pasted into script source. Deny, cancel, close, a timeout, or a
  malformed reply approves nothing. MCP elicitation, where the client reports your
  choice, is used only for clients you list in `trusted_elicitation_clients`.
- **Binding.** Each approval is tied to account, folder, UIDVALIDITY, UID, and part,
  expires after two minutes, and is used once within the same call. If the mailbox or
  the attachment's name or type changed since indexing, nothing is opened.
- **Sandbox.**
  - **macOS:** `sandbox-exec` with a deny-by-default profile. The worker may read only
    the Python runtime and installed packages. It cannot reach the network, write
    files, or read your home folder, this app's config and index, or Keychains.
  - **Linux:** `bwrap --unshare-all` with read-only binds of the same paths.
  - **Both:** empty environment, `RLIMIT_CPU`, no file writes, a wall-clock timeout,
    and an output cap.
  - **Elsewhere:** attachments are refused unless you set `attachments: ask-unsandboxed`.
- **Parsers.**
  - **PDF:** pypdf with its built-in per-stream decompression limits.
  - **Office files:** zip with a cap on every read, because declared sizes can lie, and
    XML parsed with `defusedxml`, so no entities or external references.
  - **Spreadsheets:** cell values only, never formulas.
  - **Images:** Pillow with a pixel cap checked before decoding. Images are re-created
    from pixels only, which drops EXIF, GPS, XMP, ICC, and thumbnails.
- **After extraction**, the text goes through the same classifier and redaction as
  email bodies. Attachments of withheld messages are never opened.

## Contacts and calendars

Off until an account sets `contacts: true` or `calendars: true`; a mail-only setup is
unchanged. The app-specific password also permits writes at the provider (CardDAV and
CalDAV), so what follows is about keeping this server from using that.

- **The DAV guard is the only barrier against a write.** The provider will accept a write
  from this password, so nothing outside this package stops one. `GuardedDAVConnection`
  (`dav.py`) is a subclass of `http.client.HTTPSConnection` that checks every request in
  `request`, `putrequest`, `putheader`, `endheaders` and `send`, before a byte reaches the
  socket: the method must be `PROPFIND` or `REPORT` (read-only by RFC 4918 and RFC 3253),
  the host an allowed one (the preset's contacts and calendar hosts and their `pNN-` partition
  hosts), the path plain, the body one of the fixed bodies this package builds, and the
  headers the fixed set. The DAV source refuses any connection that is not exactly that
  class. Redirects are followed by hand, only to allowed hosts, and `Authorization` goes to
  no other host. Responses have a size and time cap; one that hits it is a failure, never a
  partial result. XML goes through `defusedxml`; TLS through the OS trust store, as for mail.
  Tests assert that each of ten refused methods, sent through the source, `request`,
  `putrequest` and raw `send`, leaves the fake server's record empty. A bug here is a
  write to your contacts or calendar, and the tests are the only check; read this file.
  The guard covers the package's own request paths, the only ones that exist. It is not a
  sandbox against other code in the same process: Python code there can write to the
  connection's socket directly (`conn.sock.sendall`), call `http.client.HTTPSConnection.send`
  unbound on a guarded connection, or replace the method that opens the socket (the test
  server does exactly that to reach `127.0.0.1`). Such code would have to be in the server
  process already; the guard keeps this package's own code, and data the servers send, from
  causing a write.
- **Calendar content is writable by strangers.** Anyone can send an invitation, and a
  calendar service often puts it in your calendar unasked. Titles, locations, descriptions, organizer and
  attendee names, and repeat rules are all hostile input, and prompt injection can arrive
  that way as it does in mail. Every field is decoded and normalized first, then classified
  and redacted by the field policy (`fieldpolicy.py`, its own version, separate from the mail
  sanitizer): HTML entities are decoded and, in notes and descriptions, markup is reduced
  to its visible text first, as for mail; free text gets the full mail redaction and keyword
  withholding, an organizer in a blocked sender domain (its address or its `EMAIL`
  parameter) withholds the description, and a field that cannot be decoded, is in a
  charset outside a fixed list, or is over 100,000 characters is withheld, never returned
  raw or classified in part. Times and repeat rules are parsed into validated values and
  rendered by this code; raw text is never stored or shown. Results carry an
  untrusted-content line of their own.
- **Phone numbers, addresses and birthdays are shown (an exception to redaction).** Mail
  masks digit runs, so phone numbers and postal codes would be `[code hidden]`; a contact
  card without them is useless, so they are kept, but only in validated shapes; anything
  else is dropped. Exactly what is kept, so exactly what a code could hide in:
  - a phone (`TEL`): ASCII digits only, 7 to 15 of them counting an extension, with spaces,
    dots, hyphens, brackets and an optional leading `+`. So `TEL:4829135` is kept as a phone
    number: a 7-digit code in a phone field shows. Invisible format characters inside it are
    removed and the number kept; look-alike digits drop it.
  - an address (`ADR`): every token of digits under 16 characters (`12`, `#1204`, `12-14`,
    `482913`) is kept as a house number or PO box, in any script; other tokens get the mail
    redaction. The postal code must be ASCII letters and digits, 2 to 8 of them, plus an
    optional second group of 1 to 4 after a space or hyphen (10 characters at most).
  - a birthday (`BDAY`): rendered as a date, its year between 1900 and next year (Apple's
    "no year" marker is read as no year); any other year drops it, so `BDAY:48290131` is not
    shown as `4829-01-31`. Month and day always show.
  They go to your AI provider with everything else the assistant reads. Free-text notes and
  event text still get the mail redaction, and runs of 11 or more digits are masked in every
  text field. In single-line fields (names, titles, locations, labels) whitespace is joined
  before redaction, so a tab or line break between the groups of a code does not hide it.
- **The expansion worker has limits, and they differ by platform.** Repeat rules are
  expanded in a child process that receives validated values only, never iCalendar text,
  with an empty environment, no credentials and a wall-clock kill. The values are capped (a
  start of 1900 or later, daily or coarser, `UNTIL` clamped to the window, at most 100,000
  occurrences looked at), but the caps alone do not bound the work: a rule that never
  matches (`FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30`) makes the library search to the year 9999.
  What bounds it is a time limit per event inside the worker (macOS and Linux: one second,
  after which that event alone is marked "took too long") and the wall-clock kill of the
  child, the only bound on Windows. It runs through the same runner as the attachment
  worker. Where that runner can sandbox (macOS `sandbox-exec`, Linux `bwrap`), it has no
  network, no file writes and no access to your files, config or index. On macOS and Linux
  it also has a CPU limit. Elsewhere it is a plain child process, which is **not a
  sandbox**: on other POSIX systems with the CPU and per-event limits, on Windows with the
  wall-clock kill only. An event is marked incomplete only for what the worker reports
  about it, or when it is killed while tried alone; a worker that gives no usable answer
  at all (it cannot start, say, as when bubblewrap is installed but cannot create its
  namespaces) marks no event: they wait for the next sync and the calendar source shows
  the error in `status` and `list_accounts`. A sync spends at most 60 seconds on
  expansion, and the contacts and calendar sync takes its own lock after the mail sync has
  finished, so a stuck expansion never delays mail.
- **Incomplete events are named, not hidden.** An event whose timing the server cannot fully
  place is kept, indexed at its first occurrence and named in every date search it could
  touch, so a gap never reads as a free day. Windows time-zone names (common in Outlook
  invitations) are read as floating time and marked incomplete. On Windows the machine's
  own zone, used for floating and all-day times and for every time shown, comes from the
  registry's current rule, which is applied to every year: in a year whose daylight-saving
  dates differed, such times are shown an hour off near the change.
- **Calendars are chosen by name.** `calendars: { allow, deny }` compares names ignoring
  case, accents and spacing; a name that matches no calendar is listed by `check`, since a
  mistyped `deny` would leave that calendar indexed.
- **Second index file.** `dav-*.sqlite`, beside the mail index, is named by its own
  fingerprint (field policy version, the calendar timing parser's version, hosts, rules,
  which sources are on) and has the same
  owner-only modes and generations; a policy change selects a new, empty file. A DAV
  failure is recorded and never fails the mail sync.
- **Not checked against a live account yet.** The DAV guard and sync are tested against a
  simulated server built from the protocol specifications. Fastmail and Yahoo hosts are built
  in but unchecked.
- Google contacts and calendar are not supported (OAuth, a non-goal).

## Residual risks (documented, not solved)

- **Privacy from your AI provider.** Whatever the assistant reads is sent to it. The
  filters remove credentials, not personal information. With contacts on, that includes
  names, phone numbers and addresses of people who never agreed to it.
- **The filters are heuristic.** Homoglyphs, codes spelled out as words ("four eight
  two…"), or a code split across lines can get through. Please report bypasses (see
  SECURITY.md).
- **Over-redaction is deliberate.** In mail, phone numbers, ZIP codes, order numbers, and
  account numbers are often masked as `[code hidden]`. Amounts with a currency sign,
  years, and ISO dates are kept. Contact cards and events are the exception: validated
  phone numbers and postal addresses are shown (see [Contacts and
  calendars](#contacts-and-calendars)).
- **Images are not redacted.** A code, QR code, or screenshot of a login page in an
  approved image reaches the assistant. The dialog warns when images are included.
- **The index is a second copy of your mail** (and of your contacts and calendar, if on).
  It is sanitized but still personal, so keep it on an encrypted disk (FileVault,
  BitLocker, LUKS).
- **Desktop-automation tools.** An assistant that can also click on your screen
  (computer use, browser automation) could approve its own dialog. Human approval only
  means something when the assistant has no such tools.
- **Same-user shell agents.** An AI agent with a shell running as your OS user
  defeats everything here (see next section).
- **Memory.** "Never stored" for attachments means the app never writes them to disk.
  OS swap and crash dumps are outside its control.

## Recommended setup

**Finding (2026-11-06):** on macOS, a Keychain item's access list trusts the Python
binary that created it. Any later process using that same Python can read the password
**with no prompt**. An AI coding agent with a shell, running as your user, could read
your app password with one line of Python. That password gives full read, write, and
delete access to your mailbox at the provider, bypassing this server. The same agent
could also edit this server's code or rules. **Your OS user account is the security
boundary.**

| Your situation | Setup |
|---|---|
| You use AI only through chat clients whose only access to your machine is MCP tools (Claude Desktop, or Cursor and VS Code chat with terminal and file tools off) | **Standard install** under your own user, with the password in the OS keyring. The tools never return credentials. |
| A shell- or file-capable AI agent also runs under your user | **Dedicated-user install (recommended).** See below. |
| You can't create another OS user | Standard install. Keep agents out of the session where mail is set up, and **revoke the app password if an agent ever had unsupervised shell access.** |

### Dedicated-user install (macOS / Linux)

> These steps have not yet been tested end to end. Please report problems.

```sh
# 1. Create an unprivileged, non-admin user (macOS: System Settings → Users & Groups,
#    standard account; Linux: sudo useradd -m mailmcp).
# 2. As that user, install and configure:
sudo -u mailmcp -i
uv tool install git+https://github.com/pkellner/readonly-safe-mail-mcp@v0.3.1
mkdir -p ~/.config/readonly-safe-mail-mcp && $EDITOR ~/.config/readonly-safe-mail-mcp/accounts.yaml
readonly-safe-mail-mcp set-password icloud --store file   # mode-600 file in that user's home
readonly-safe-mail-mcp check && readonly-safe-mail-mcp sync
exit
# 3. Let your account launch only the server, nothing else, as that user:
echo "$USER ALL=(mailmcp) NOPASSWD: /Users/mailmcp/.local/bin/readonly-safe-mail-mcp serve" \
  | sudo tee /etc/sudoers.d/mailmcp && sudo chmod 440 /etc/sudoers.d/mailmcp
# 4. In your MCP client, use: command "sudo", args ["-n", "-u", "mailmcp",
#    "/Users/mailmcp/.local/bin/readonly-safe-mail-mcp", "serve"]
```

The `file` credential backend is used because a user with no GUI session has no login
Keychain. The server refuses the file unless it is owned by that user with mode 600.
`check` warns when config, credentials, the index, or the server's code can be read or
changed by other users.

The attachment approval dialog needs a GUI session. A dedicated user running headless
cannot show it, so attachments are refused there unless you list your client in
`trusted_elicitation_clients`.

`delete-password` removes the local copy. Revoking the app password at your provider is
the only way to invalidate it everywhere.
