#!/bin/bash
# One-step installer for readonly-safe-mail-mcp on macOS with one iCloud account.
#
#   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/pkellner/readonly-safe-mail-mcp/main/install.sh)"
#
# Safe to run again at any time. If it finds an earlier setup it shows it and asks
# what you want: keep it (re-test and reconnect), change the email address, enter a
# new password, or start over. To remove everything, use uninstall.sh.
#
# What it does, in order (read it before running it):
#   1. Installs uv (https://docs.astral.sh/uv/) if missing. uv brings its own Python;
#      no Homebrew, Xcode, git, or system Python is needed or changed, and your shell
#      startup files are not edited.
#   2. Installs readonly-safe-mail-mcp (pinned version below) with Python 3.13.
#   3. Asks for your iCloud address and writes ~/.config/readonly-safe-mail-mcp/accounts.yaml.
#   4. Opens Apple's page to make an app-specific password, then stores it in your
#      login Keychain. The password never touches disk or this script's variables.
#   5. Runs a read-only test login (and helps you fix it if it fails).
#   6. Connects Claude Code and/or Claude Desktop, whichever is installed.
#   7. Starts the first sync (safe to stop with Ctrl-C; it resumes later).
set -euo pipefail

VERSION="0.2.2"
RAW="https://raw.githubusercontent.com/pkellner/readonly-safe-mail-mcp/main"
PKG="https://github.com/pkellner/readonly-safe-mail-mcp/archive/refs/tags/v${VERSION}.tar.gz"
NAME="readonly-safe-mail-mcp"
SERVER="readonly-safe-mail"
ACCOUNT="icloud"
CONFIG_DIR="${RSMM_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/$NAME}"
DATA_DIR="${RSMM_DATA_DIR:-$HOME/Library/Application Support/$NAME}"
ACCOUNTS="$CONFIG_DIR/accounts.yaml"
UV_MARKER="$CONFIG_DIR/.installer-installed-uv"
DESKTOP_DIR="$HOME/Library/Application Support/Claude"
DESKTOP_CONFIG="$DESKTOP_DIR/claude_desktop_config.json"
INSTALL_CMD="/bin/bash -c \"\$(curl -fsSL $RAW/install.sh)\""
UNINSTALL_CMD="/bin/bash -c \"\$(curl -fsSL $RAW/uninstall.sh)\""

# ---------------------------------------------------------------- output helpers
B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'
say()  { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
note() { printf '  • %s\n' "$*"; }
warn() { printf '  %s! %s%s\n' "$Y" "$*" "$N"; }
fail() {
  printf '\n%sError: %s%s\n' "$R" "$1" "$N" >&2
  shift; for line in "$@"; do printf '  %s\n' "$line" >&2; done
  exit 1
}
stopped() {
  printf '\n\n%sStopped.%s Nothing is broken. To pick up where you left off, run the\n' "$Y" "$N"
  printf 'install command again:\n  %s\n' "$INSTALL_CMD"
  exit 130
}
unexpected() {
  printf '\n%sSomething unexpected went wrong (install.sh line %s).%s\n' "$R" "$1" "$N" >&2
  printf 'Run the install command again; it is safe to repeat:\n  %s\n' "$INSTALL_CMD" >&2
  printf 'If it happens again, send the messages above (no passwords) to whoever sent you this.\n' >&2
}
trap stopped INT
trap 'unexpected $LINENO' ERR

# ask PROMPT: reads one line from the keyboard into REPLY (trimmed).
ask() {
  if ! read -r -p "$1" REPLY </dev/tty; then
    printf '\n'; stopped
  fi
  REPLY="${REPLY#"${REPLY%%[![:space:]]*}"}"
  REPLY="${REPLY%"${REPLY##*[![:space:]]}"}"
}
# yesno PROMPT DEFAULT(y|n): returns 0 for yes. Re-asks until it gets an answer.
yesno() {
  local hint="[y/N]"; [ "$2" = y ] && hint="[Y/n]"
  while :; do
    ask "$1 $hint "
    case "${REPLY:-$2}" in
      [yY]|[yY][eE][sS]) return 0 ;;
      [nN]|[nN][oO]) return 1 ;;
    esac
    warn "Please type y for yes or n for no."
  done
}

# ---------------------------------------------------------------- checks
[ "$(uname -s)" = "Darwin" ] || fail "this installer is for macOS only." "See the README for other systems."
{ : </dev/tty; } 2>/dev/null || fail "this installer needs to be run in a Terminal window." \
  "Open Terminal (Applications -> Utilities), paste the install command, and press Return."

cat <<EOF

${B}readonly-safe-mail-mcp installer${N} (version $VERSION)

This tool lets Claude search a private, read-only copy of your iCloud mail that is
kept on this Mac. It has ${B}never had an independent security audit${N}. Use it only if
you accept that risk: https://github.com/pkellner/readonly-safe-mail-mcp#readme
EOF
if ! yesno "Continue?" n; then
  printf '\nCancelled. Nothing was installed or changed.\n'
  exit 0
fi

# ---------------------------------------------------------------- earlier setup?
cur_email=""; custom=""
if [ -f "$ACCOUNTS" ]; then
  cur_email="$(sed -n 's/^  icloud: { preset: icloud, username: "\(.*\)" }$/\1/p' "$ACCOUNTS" | head -1)"
  [ -n "$cur_email" ] || custom=1
fi
pw_saved() {
  security find-generic-password -s "$NAME" -a "$ACCOUNT" >/dev/null 2>&1 ||
    [ -s "$CONFIG_DIR/credentials/$ACCOUNT" ]
}
had_pw=""; pw_saved && had_pw=1
had_data=""; [ -d "$DATA_DIR" ] && had_data=1

action="fresh"
if [ -f "$ACCOUNTS" ] || [ -n "$had_pw" ] || [ -n "$had_data" ]; then
  say "You already have this set up"
  if [ -n "$custom" ]; then
    note "Settings:  $ACCOUNTS (edited by hand; this installer will not change it)"
  else
    note "Email:     ${cur_email:-not set yet}"
  fi
  if [ -n "$had_pw" ]; then note "Password:  saved in your Keychain"; else note "Password:  not saved yet"; fi
  if [ -n "$had_data" ]; then
    note "Mail data: $(du -sh "$DATA_DIR" 2>/dev/null | cut -f1 | tr -d ' ') downloaded to $DATA_DIR"
  else
    note "Mail data: nothing downloaded yet"
  fi
  echo
  echo "What would you like to do?"
  echo "  1) Keep everything, re-test and reconnect (recommended)"
  if [ -z "$custom" ]; then
    echo "  2) Change the email address"
    echo "  3) Enter a new app-specific password"
  fi
  echo "  4) Start over: erase the settings, saved password and downloaded mail, then set up again"
  echo "  5) Quit without changing anything"
  while :; do
    ask "Type a number and press Return [1]: "
    case "${REPLY:-1}" in
      1) action="keep"; break ;;
      2) [ -z "$custom" ] && { action="email"; break; } ;;
      3) [ -z "$custom" ] && { action="password"; break; } ;;
      4) if yesno "This permanently deletes your settings, saved password and downloaded mail on this Mac (your mail on iCloud is not touched). Erase and start over?" n; then
           action="fresh-erase"; break
         fi
         echo "OK, nothing was erased. Choose again." ; continue ;;
      5) printf '\nNothing was changed.\n'; exit 0 ;;
    esac
    warn "\"$REPLY\" is not one of the choices. Type just the number, for example 1."
  done
fi

# ---------------------------------------------------------------- 1. uv
export PATH="$HOME/.local/bin:$PATH"
say "Step 1 of 6: uv (installs and manages Python for this tool)"
if command -v uv >/dev/null 2>&1; then
  ok "uv is already installed ($(command -v uv))"
else
  note "Downloading uv from astral.sh..."
  # UV_NO_MODIFY_PATH: don't edit ~/.zshrc and friends; this script sets PATH itself.
  if ! curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null; then
    fail "uv could not be downloaded or installed." \
      "Check your internet connection, then run the install command again:" "$INSTALL_CMD"
  fi
  command -v uv >/dev/null 2>&1 || fail "uv seemed to install, but it can't be found." \
    "Run the install command again. If that doesn't help, see https://docs.astral.sh/uv/"
  mkdir -p "$CONFIG_DIR" && chmod 700 "$CONFIG_DIR" && : > "$UV_MARKER"
  ok "Installed uv"
fi

# ---------------------------------------------------------------- 2. the tool
say "Step 2 of 6: readonly-safe-mail-mcp $VERSION"
note "Installing or updating (the first time, this downloads Python 3.13 and can take a minute)..."
if ! uv tool install --quiet --force --python 3.13 "$PKG"; then
  fail "the tool could not be installed (see the message above)." \
    "This is usually a network problem. Check your connection and run the install command again:" \
    "$INSTALL_CMD"
fi
BIN="$(uv tool dir --bin)/$NAME"
PY="$(uv tool dir)/$NAME/bin/python"
[ -x "$BIN" ] || fail "the install finished, but $BIN is missing." "Run the install command again."
ok "Installed $NAME $VERSION"

# ---------------------------------------------------------------- erase (start over)
account_names() {
  [ -f "$ACCOUNTS" ] || return 0
  "$PY" -I -c 'import sys, yaml; print("\n".join(yaml.safe_load(open(sys.argv[1]))["accounts"]))' \
    "$ACCOUNTS" 2>/dev/null || echo "$ACCOUNT"
}
if [ "$action" = "fresh-erase" ]; then
  say "Erasing the old setup"
  for acct in $(account_names) "$ACCOUNT"; do
    "$BIN" delete-password "$acct" >/dev/null 2>&1 || true
  done
  if pw_saved; then
    warn "The old password is still in your Keychain. You can delete it later in Keychain Access"
    warn "(search for $NAME). Setting a new password below replaces it anyway."
  else
    ok "Removed the saved password from your Keychain"
  fi
  uv_mark=""; [ -f "$UV_MARKER" ] && uv_mark=1
  rm -rf "$CONFIG_DIR" && ok "Removed the settings ($CONFIG_DIR)"
  rm -rf "$DATA_DIR" && ok "Removed the downloaded mail ($DATA_DIR)"
  [ -z "$uv_mark" ] || { mkdir -p "$CONFIG_DIR" && chmod 700 "$CONFIG_DIR" && : > "$UV_MARKER"; }
  cur_email=""; custom=""
  action="fresh"
fi

# ---------------------------------------------------------------- 3. email
EMAIL_RE='^[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$'
ask_email() {
  local e
  while :; do
    if [ -n "$cur_email" ]; then
      ask "Your iCloud email address [$cur_email] (press Return to keep it): "
      e="${REPLY:-$cur_email}"
    else
      ask "Your iCloud email address (for example you@icloud.com): "
      e="$REPLY"
    fi
    if [ -z "$e" ]; then
      warn "Nothing was entered. Type your iCloud email address, then press Return."
      continue
    fi
    e="$(printf '%s' "$e" | tr '[:upper:]' '[:lower:]')"
    if ! [[ "$e" =~ $EMAIL_RE ]]; then
      warn "\"$e\" doesn't look like an email address. It should look like you@icloud.com."
      continue
    fi
    case "${e##*@}" in
      icloud.com|me.com|mac.com) ;;
      *)
        warn "\"$e\" is not an @icloud.com, @me.com or @mac.com address."
        warn "iCloud Mail needs your iCloud Mail address, even if you sign in to Apple with another"
        warn "one. On a Mac it is under System Settings -> [your name] -> iCloud -> iCloud Mail."
        yesno "Use \"$e\" anyway?" n || continue ;;
    esac
    EMAIL="$e"; return 0
  done
}
write_config() {
  local old="$cur_email"
  mkdir -p "$CONFIG_DIR" && chmod 700 "$CONFIG_DIR"
  (umask 077; printf 'accounts:\n  icloud: { preset: icloud, username: "%s" }\n' "$EMAIL" > "$ACCOUNTS")
  cur_email="$EMAIL"
  if [ -n "$old" ] && [ "$old" != "$EMAIL" ] && [ -d "$DATA_DIR" ]; then
    rm -rf "$DATA_DIR"
    ok "Removed the mail downloaded for $old"
  fi
  ok "Saved your email address: $EMAIL"
}

need_email=""; need_pw=""
case "$action" in
  fresh)    need_email=1; need_pw=1 ;;
  email)    need_email=1 ;;
  password) need_pw=1 ;;
esac
if [ -z "$custom" ]; then
  [ -n "$cur_email" ] || need_email=1
  pw_saved || need_pw=1
fi

say "Step 3 of 6: your iCloud email address"
if [ -n "$custom" ]; then
  ok "Using your hand-edited settings in $ACCOUNTS"
elif [ -n "$need_email" ]; then
  [ "$action" = "fresh" ] || [ -z "$cur_email" ] || note "Current address: $cur_email"
  ask_email
  if [ -n "$cur_email" ] && [ "$cur_email" != "$EMAIL" ]; then
    if [ -d "$DATA_DIR" ] &&
      ! yesno "Mail already downloaded for $cur_email will be erased from this Mac. Continue?" y; then
      printf '\nNothing was changed. Your setup still uses %s.\n' "$cur_email"; exit 0
    fi
    need_pw=1
    note "A new address needs its own app-specific password, so you'll enter one next."
  fi
  write_config
else
  ok "Keeping $cur_email"
fi

# ---------------------------------------------------------------- 4. password
enter_password() {
  cat <<EOF

You need an ${B}app-specific password${N} from Apple (not your regular Apple password).
  1. Your browser will open account.apple.com. Sign in.
  2. Go to Sign-In and Security -> App-Specific Passwords -> + (or Generate).
  3. Name it "mail mcp" and copy the password it shows. It looks like abcd-efgh-ijkl-mnop.
  4. Come back to this window. It will find the password you copied and ask
     whether to use it. Answer 1 and press Return.
     If you would rather type or paste it yourself, answer 2.
     ${B}Nothing appears on screen while you type or paste. That is normal.${N}
EOF
  ask "Press Return to open Apple's page... "
  open "https://account.apple.com/account/manage" 2>/dev/null ||
    note "Couldn't open the browser. Go to https://account.apple.com yourself."
  local tries=0
  until "$BIN" set-password "$ACCOUNT" </dev/tty; do
    tries=$((tries + 1))
    if [ "$tries" -ge 3 ]; then
      fail "no password was saved." \
        "When you have your app-specific password, run the install command again:" "$INSTALL_CMD"
    fi
    warn "Let's try that again. Copy the password from Apple's page first, or type it (it stays invisible)."
  done
  ok "Saved the password in your Keychain"
}

say "Step 4 of 6: Apple app-specific password"
if [ -n "$custom" ]; then
  ok "Using the passwords for your hand-edited settings"
elif [ -n "$need_pw" ]; then
  enter_password
else
  ok "Keeping the password already saved in your Keychain"
fi

# ---------------------------------------------------------------- 5. test login
say "Step 5 of 6: testing a read-only login to iCloud"
until "$BIN" check; do
  echo
  warn "The test login did not work. The usual causes are:"
  warn "  - a typo in the email address (${cur_email:-see $ACCOUNTS})"
  warn "  - your regular Apple password was used instead of an app-specific one"
  warn "  - the app-specific password was revoked, or you changed your Apple password since"
  warn "  - no internet connection"
  [ -z "$custom" ] || fail "the test login failed for your hand-edited settings." \
    "Fix $ACCOUNTS or the passwords, then run the install command again."
  echo "What would you like to do?"
  echo "  1) Enter a new app-specific password (recommended)"
  echo "  2) Change the email address"
  echo "  3) Try the test again (for example, after fixing the internet connection)"
  echo "  4) Stop for now (your settings are kept; run the installer again later)"
  while :; do
    ask "Type a number and press Return [1]: "
    case "${REPLY:-1}" in
      1) enter_password; break ;;
      2) ask_email; write_config; enter_password; break ;;
      3) break ;;
      4) printf '\nStopped. When you are ready, run the install command again:\n  %s\n' "$INSTALL_CMD"; exit 1 ;;
    esac
    warn "\"$REPLY\" is not one of the choices. Type just the number, for example 1."
  done
  say "Testing the login again"
done
ok "Login works. Your mail was opened read-only."

# ---------------------------------------------------------------- 6. connect Claude
say "Step 6 of 6: connecting Claude"
connected=""; restart=""
if command -v claude >/dev/null 2>&1; then
  claude mcp remove --scope user "$SERVER" >/dev/null 2>&1 || true
  if out="$(claude mcp add --scope user "$SERVER" -- "$BIN" serve 2>&1)"; then
    ok "Claude Code: connected"; connected=1
    restart="${restart:+$restart; }start a new Claude Code session"
  else
    warn "Claude Code: could not connect it automatically:"
    printf '      %s\n' "$out"
    warn "To do it by hand: claude mcp add --scope user $SERVER -- \"$BIN\" serve"
  fi
else
  note "Claude Code: not installed, skipped"
fi
if [ -d "$DESKTOP_DIR" ]; then
  if "$PY" -I - "$DESKTOP_CONFIG" "$SERVER" "$BIN" 2>/dev/null <<'PYEOF'
import json, os, shutil, sys
path, name, binary = sys.argv[1:]
cfg = {}
if os.path.exists(path):
    with open(path) as f:
        text = f.read()
    cfg = json.loads(text) if text.strip() else {}
    shutil.copy2(path, path + ".bak")
cfg.setdefault("mcpServers", {})[name] = {"command": binary, "args": ["serve"]}
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
PYEOF
  then
    ok "Claude Desktop: connected"; connected=1
    restart="${restart:+$restart; }quit Claude Desktop (Cmd-Q) and open it again"
  else
    warn "Claude Desktop: could not update $DESKTOP_CONFIG (left unchanged)."
    warn "The file may not be valid JSON. See the README to add the server by hand."
  fi
else
  note "Claude Desktop: not installed, skipped"
fi

# ---------------------------------------------------------------- done + first sync
say "All set"
ok "Email:     ${cur_email:-from $ACCOUNTS}"
ok "Password:  saved in your Keychain"
ok "Mail data: $DATA_DIR (stays on this Mac)"
if [ -n "$connected" ]; then
  printf '\n%sNext:%s\n' "$B" "$N"
  printf '%s\n' "$restart" | tr ';' '\n' | sed 's/^ *//' | while IFS= read -r line; do
    printf '  - %s%s.\n' "$(printf '%s' "${line:0:1}" | tr '[:lower:]' '[:upper:]')" "${line:1}"
  done
  printf 'Then ask Claude something like: "Find the emails about my upcoming trip."\n'
else
  printf '\n%sNo Claude app was found, so nothing was connected.%s\n' "$B" "$N"
  printf '  - Using Codex? Follow README-CODEX.md: https://github.com/pkellner/readonly-safe-mail-mcp/blob/main/README-CODEX.md\n'
  printf '  - Installing Claude later? Run the install command again and choose 1.\n'
fi
# Commands are shown with the full path: this script does not put $BIN on the PATH.
# With hand-edited settings the account name is not known here: the list shows a
# placeholder, and the password shortcuts take the name when they are used.
acct_arg="$ACCOUNT"; alias_acct=" $ACCOUNT"
[ -z "$custom" ] || { acct_arg="ACCOUNT"; alias_acct=""; }
cat <<EOF

${D}Change settings later:  run the install command again
Remove everything:      $UNINSTALL_CMD

See download progress:  "$BIN" status
Test the connection:    "$BIN" check
Download mail now:      "$BIN" sync

Advanced:  show the rules in effect:   "$BIN" print-rules
Advanced:  show where files are kept:  "$BIN" paths
Advanced:  replace the saved password: "$BIN" set-password $acct_arg
Advanced:  remove the saved password:  "$BIN" delete-password $acct_arg

Optional shortcuts: to type mail-status instead of the long command, copy all of
the lines below, paste them into Terminal, press Return, then open a new Terminal
window. They add shortcuts to your ~/.zshrc file. This installer does not do it for you.

cat >> ~/.zshrc <<'MAIL_SHORTCUTS'

# $NAME shortcuts
alias mail-status='"$BIN" status'
alias mail-check='"$BIN" check'
alias mail-sync='"$BIN" sync'
alias mail-rules='"$BIN" print-rules'
alias mail-paths='"$BIN" paths'
alias mail-set-password='"$BIN" set-password$alias_acct'
alias mail-delete-password='"$BIN" delete-password$alias_acct'
MAIL_SHORTCUTS
${N}
${B}Now downloading your mail for the first time.${N} Newest mail comes first, so recent
mail is searchable within minutes; a big mailbox can take hours. You can press
Ctrl-C at any time. It picks up where it left off, and it also keeps up to date by
itself whenever Claude searches.

EOF
sync_stopped=""
trap 'sync_stopped=1' INT
sync_rc=0
"$BIN" sync </dev/tty || sync_rc=$?
trap stopped INT
echo
if [ -n "$sync_stopped" ]; then
  ok "Download paused. It resumes by itself the next time Claude searches your mail."
elif [ "$sync_rc" -ne 0 ]; then
  warn "The download stopped early (see the message above). That's OK: it resumes the next"
  warn "time Claude searches, or run: \"$BIN\" sync"
else
  ok "Your mail is downloaded. From now on only new mail is fetched, when Claude searches."
fi
note "To see download progress at any time: \"$BIN\" status"
