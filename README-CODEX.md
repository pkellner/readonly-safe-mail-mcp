# Use iCloud Mail with Codex on a Mac

## TL;DR — install and connect

1. **Install [OpenAI's desktop app](https://learn.chatgpt.com/docs/quickstart)**,
   sign in with your ChatGPT account, and open **Codex** in a local task on your Mac.
2. **Open Terminal** (Applications → Utilities) and run:

   ```sh
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/pkellner/readonly-safe-mail-mcp/main/install.sh)"
   ```

3. **Follow the installer.** Enter your iCloud email address. When it opens
   [Apple's account page](https://account.apple.com/), choose **Sign-In and Security
   → App-Specific Passwords → Generate**, name it **Mail MCP**, and paste the
   generated password into **Terminal**, never into chat. Apple requires
   two-factor authentication. The installer connects Codex by itself when it finds
   it (look for **“Codex: connected”**). Let the mail sync run.
4. **Quit Codex, open it again and start a new local task.** If the installer did
   not say “Codex: connected”, paste this into local Codex instead:

   > Connect my installed readonly-safe-mail-mcp server to Codex. Find its absolute
   > executable path and register it with the argument `serve`, preserving existing
   > MCP settings. Do not read or display passwords. Tell me if I need to restart,
   > then verify the connection by calling `list_accounts`.

5. **Restart Codex if prompted**, then ask: “Search my iCloud email for upcoming
   travel and summarize what I need to do.”

That's the setup. The full walkthrough and ready-to-send email follow below.

---

This guide is for someone who already uses ChatGPT in a web browser and wants to
search their own iCloud email using `readonly-safe-mail-mcp`. It covers installing
OpenAI's desktop app, setting up the mail server, and connecting it to local Codex.
No programming experience or Claude subscription is required.

The email below is ready to copy and send. It assumes the recipient has a Mac.
The one-step mail installer is macOS-only; iCloud on macOS is the project's tested
configuration.

## Email to send

**Subject: How to search your iCloud email with Codex**

Hi,

Here's how to set up the iCloud email tool I use. You'll be able to ask things like
“Find the emails about my upcoming trip” or “Summarize the messages from my contractor.”

The tool runs on your Mac and gives Codex a searchable, filtered copy of your mail.
It doesn't include tools to send, delete, or change messages. It removes links and
codes and withholds account-security messages. It's a personal project without an
independent security audit, so please read the
[project's limitations](https://github.com/pkellner/readonly-safe-mail-mcp#readme)
before installing. Email text that you ask Codex to read is sent to OpenAI.

### 1. Install OpenAI's desktop app

Use the download link in
[OpenAI's official setup guide](https://learn.chatgpt.com/docs/quickstart).
Install the Mac version, open it, and sign in with the same ChatGPT account you
already use in your browser.

ChatGPT Plus and Pro include Codex access, subject to usage limits. You don't need
a separate API key for this setup. You can check your plan against
[OpenAI's current plan details](https://learn.chatgpt.com/docs/pricing).

In the desktop app, select **Codex** and start a task that works on your **local
Mac**. If it asks for a folder, create an empty local folder named `mail-mcp-setup`
in your home folder and select it. This is just a place for the setup task; the
mail tool stores its index in its own local application data folder.

This setup uses Codex on your Mac. Typing the setup prompt into an ordinary browser
chat does not give that chat access to your Mac's mail server.

### 2. Install the mail tool in Terminal

Open **Terminal** from Applications → Utilities. Review the
[installer](https://github.com/pkellner/readonly-safe-mail-mcp/blob/main/install.sh),
then paste this command into Terminal and press Return:

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/pkellner/readonly-safe-mail-mcp/main/install.sh)"
```

The installer asks whether to continue, installs the required software, and asks
for your iCloud email address. You don't need to install Python, Homebrew, or Git
yourself. Enter the address you use for iCloud Mail.

Keep Terminal open. Next, the installer will help you create an Apple app-specific
password.

### 3. Create an Apple app-specific password

Your Apple Account must have **two-factor authentication** enabled. Have your
trusted Apple device or phone available to complete Apple's sign-in verification.

When the installer asks you to press Return to open Apple's website:

1. Sign in at [account.apple.com](https://account.apple.com/).
2. Open **Sign-In and Security** → **App-Specific Passwords**.
3. Select **Generate an app-specific password** (or the **+** button).
4. Give it a name such as **iCloud Mail MCP** and follow Apple's prompts.
5. Copy the generated password.
6. Return to Terminal. At the **App-specific password for …** prompt, paste it
   and press Return. Nothing appears while you paste or type; that is normal.

Use the generated app-specific password here, not your normal Apple Account
password. Enter your normal Apple Account password only on Apple's own sign-in
page when Apple requests it. Don't paste either password into a chat or email.

The installer stores the app-specific password in your Mac's Keychain. Apple's
[app-specific password instructions](https://support.apple.com/en-us/102654)
explain the process if the screen looks different.

### 4. Let the installer check and sync your mail

The installer tests the connection and starts copying mail into a local search
index, newest messages first. Recent mail should become available before the
entire mailbox finishes. A large mailbox can take hours.

You can leave Terminal running while you do the next step. If you need to stop
the sync, press **Control-C**; progress is retained and syncing can resume later.

In its last step the installer connects the apps it finds. Look for
**“Codex: connected”**. If you see it, quit Codex, open it again, start a new
local task, and skip to step 6. If you see **“Codex: not installed, skipped”** or
a warning, connect it in the next step. “Claude Code: not installed, skipped” is
expected if you don't use Claude; you do not need to install it.

### 5. Ask Codex to connect the installed mail server (only if the installer did not)

Paste this into the local Codex task you opened in step 1:

> I have installed readonly-safe-mail-mcp on this Mac using the installer from
> https://github.com/pkellner/readonly-safe-mail-mcp. I configured my iCloud
> account and entered an Apple app-specific password directly in Terminal.
>
> Please connect the installed server to Codex. Read the repository's instructions
> and check current Codex MCP configuration documentation. Locate the installed
> readonly-safe-mail-mcp executable and use its absolute path with the argument
> `serve`. Preserve my other MCP settings and reuse an existing matching entry
> if one is already configured. Use `codex mcp add` if the CLI is available;
> otherwise configure the equivalent stdio entry in `~/.codex/config.toml`.
>
> Check the local sync status. Don't read or display credentials or ask me to
> paste a password into chat. If a password must be re-entered, give me the command
> to run myself in Terminal. Tell me whether I need to restart the app or start a
> new task. Once the connection is loaded, call `list_accounts` through MCP to
> verify it works. Explain any remaining steps in plain English.

Codex may ask permission to update its configuration. After it finishes, follow
its restart instructions, then start a new local Codex task and ask:

> Use readonly-safe-mail to list my configured accounts and tell me how many
> messages are indexed and whether the index is up to date.

A successful response should show your iCloud account and its indexed message
count. A small count during the first sync is normal.

### 6. Start asking about your email

For example:

> Search my iCloud email for messages about upcoming travel. Summarize the dates,
> reservations, and anything I still need to do.

Or:

> Find emails from my contractor in the last month and summarize outstanding
> questions.

Use local Codex on the same Mac for these questions. Searches can trigger a
background sync when the index is out of date. If you ask to open an attachment,
the tool asks for your approval in a separate dialog.

If something fails, send me the error text, with any personal information removed.
Never send me your password.

Peter

---

## Reference: connect Codex manually

The installer does this for you when it finds Codex. These steps are an
alternative, after the mail installer has completed. They do not install the mail tool or configure the iCloud account.

If the `codex` command is installed, run these commands in Terminal:

```sh
export PATH="$HOME/.local/bin:$PATH"
command -v readonly-safe-mail-mcp
codex mcp add readonly_safe_mail -- "$(command -v readonly-safe-mail-mcp)" serve
codex mcp get readonly_safe_mail
```

Before running `codex mcp add`, check that `command -v` printed an absolute path.
If it printed nothing, locate the executable first; don't register an empty
command. `codex mcp add` writes the global configuration, so first check for an
existing `readonly_safe_mail` entry if this is not a fresh setup.

If `codex` is not on your shell's PATH, the desktop app can still be configured
through `~/.codex/config.toml`. Add this entry, preserving existing settings and
avoiding a duplicate table:

```toml
[mcp_servers.readonly_safe_mail]
command = "/Users/YOUR_MAC_USERNAME/.local/bin/readonly-safe-mail-mcp"
args = ["serve"]
```

Replace the example command with the actual absolute path found on that Mac.
Don't put the Apple password in this file; the server uses the Keychain entry
created by the installer. Restart the app and open a new local task to load the
connection. Registration alone does not prove the server works: verify it by
asking Codex to call `list_accounts`.

See [OpenAI's MCP documentation](https://developers.openai.com/codex/mcp).

## Troubleshooting

- **Apple doesn't show App-Specific Passwords:** check that two-factor
  authentication is enabled for the Apple Account.
- **Login fails:** check the iCloud address and generate a fresh app-specific
  password. To replace the stored password, run
  `~/.local/bin/readonly-safe-mail-mcp set-password icloud` yourself in Terminal,
  then run `~/.local/bin/readonly-safe-mail-mcp check`. Adjust the executable path
  if your installation uses a different location.
- **You changed your main Apple Account password:** Apple revokes existing
  app-specific passwords when the primary password changes or is reset. Generate
  a replacement and store it with `set-password icloud`.
- **Login fails with `[UNAVAILABLE] Service temporarily unavailable`:** iCloud is
  temporarily refusing this kind of login, usually after a lot of downloading. It is
  not a password problem. Wait a few hours to a day, then test with
  `~/.local/bin/readonly-safe-mail-mcp check`. Syncing resumes by itself once it
  logs in again.
- **Codex doesn't see the mail tools:** confirm this is a local task on the Mac
  where you installed the server, check the executable path, and restart the app.
- **Older messages are missing:** let the first sync finish. Run
  `~/.local/bin/readonly-safe-mail-mcp status` to see progress, or
  `~/.local/bin/readonly-safe-mail-mcp sync` to resume a stopped sync.
- **You typed something wrong in the installer:** run the install command again.
  It shows your current setup and lets you change the email address, enter a new
  password, or start over.
- **You want to remove it:** run this in Terminal. It removes the tool, its
  settings, the saved password, all downloaded mail, and the Codex entry (your
  other Codex settings are kept), then tells you what it did:

  ```sh
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/pkellner/readonly-safe-mail-mcp/main/uninstall.sh)"
  ```

  Then revoke the **iCloud Mail MCP** app-specific password at Apple's website
  and restart Codex.

## Limits of this setup

The server's read-only restrictions apply to its own mail tools. A coding agent
running as your Mac user can have broader shell and file access; the server is
not a security boundary against that agent. Read the
[threat model](docs/THREAT_MODEL.md), including the dedicated-user option, before
deciding whether this setup meets your needs.

The browser interface needs a separate connection to reach a local server. OpenAI
documents a [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
route, which requires a local tunnel client, a Platform runtime API key, and the
appropriate account/workspace access. This guide and the current installer do
not configure that route.
