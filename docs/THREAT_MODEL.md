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

## Residual risks (documented, not solved)

- **Privacy from your AI provider.** Whatever the assistant reads is sent to it. The
  filters remove credentials, not personal information.
- **The filters are heuristic.** Homoglyphs, codes spelled out as words ("four eight
  two…"), or a code split across lines can get through. Please report bypasses (see
  SECURITY.md).
- **Over-redaction is deliberate.** Phone numbers, ZIP codes, order numbers, and
  account numbers are often masked as `[code hidden]`. Amounts with a currency sign,
  years, and ISO dates are kept.
- **Images are not redacted.** A code, QR code, or screenshot of a login page in an
  approved image reaches the assistant. The dialog warns when images are included.
- **The index is a second copy of your mail.** It is sanitized but still personal, so
  keep it on an encrypted disk (FileVault, BitLocker, LUKS).
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
uv tool install git+https://github.com/pkellner/readonly-safe-mail-mcp@v0.2.1
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
