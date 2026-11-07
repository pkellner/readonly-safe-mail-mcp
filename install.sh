#!/bin/bash
# One-step installer for readonly-safe-mail-mcp on macOS with one iCloud account.
#
#   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/pkellner/readonly-safe-mail-mcp/main/install.sh)"
#
# What it does, in order (read it before running it):
#   1. Installs uv (https://docs.astral.sh/uv/) if missing. uv brings its own Python;
#      no Homebrew, Xcode, git, or system Python is needed or changed.
#   2. Installs readonly-safe-mail-mcp (pinned version below) with Python 3.13.
#   3. Asks for your iCloud address and writes ~/.config/readonly-safe-mail-mcp/accounts.yaml
#      (skipped if that file already exists).
#   4. Opens Apple's page to make an app-specific password, then stores it in your
#      login Keychain. The password never touches disk or this script's variables.
#   5. Runs a read-only test login.
#   6. Connects Claude Code and/or Claude Desktop, whichever is installed.
#   7. Starts the first sync (safe to stop with Ctrl-C; it resumes later).
set -euo pipefail

VERSION="0.2.0"
PKG="https://github.com/pkellner/readonly-safe-mail-mcp/archive/refs/tags/v${VERSION}.tar.gz"
NAME="readonly-safe-mail-mcp"
SERVER="readonly-safe-mail"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$NAME"
DESKTOP_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }
ask()  { local reply; read -r -p "$1" reply </dev/tty; printf '%s' "$reply"; }

[ "$(uname -s)" = "Darwin" ] || fail "this installer is for macOS. See the README for other systems."
[ -r /dev/tty ] || fail "run this from a Terminal window."

cat <<'EOF'

readonly-safe-mail-mcp has NEVER had an independent security audit.
Use it only if you have reviewed the code and accept the risk.
https://github.com/pkellner/readonly-safe-mail-mcp#readme
EOF
case "$(ask 'Continue? [y/N] ')" in [yY]*) ;; *) echo "Cancelled."; exit 0 ;; esac

# 1. uv
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (manages Python for this tool)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  command -v uv >/dev/null 2>&1 || fail "uv did not install. See https://docs.astral.sh/uv/"
fi

# 2. the tool
say "Installing $NAME $VERSION (uv downloads Python 3.13 if needed)"
uv tool install --force --python 3.13 "$PKG"
BIN="$(uv tool dir --bin)/$NAME"
[ -x "$BIN" ] || fail "install finished but $BIN was not found."

# 3. account config
if [ -f "$CONFIG_DIR/accounts.yaml" ]; then
  say "Keeping your existing $CONFIG_DIR/accounts.yaml"
else
  email="$(ask 'Your iCloud email address (e.g. name@icloud.com): ')"
  case "$email" in *@*.*) ;; *) fail "that doesn't look like an email address." ;; esac
  mkdir -p "$CONFIG_DIR" && chmod 700 "$CONFIG_DIR"
  (umask 077; printf 'accounts:\n  icloud: { preset: icloud, username: "%s" }\n' "$email" \
    > "$CONFIG_DIR/accounts.yaml")
fi

# 4. app-specific password
say "Create an app-specific password"
cat <<'EOF'
Your browser will open account.apple.com. Sign in, then go to
  Sign-In and Security -> App-Specific Passwords -> +
Name it "mail mcp" and copy the password it shows (xxxx-xxxx-xxxx-xxxx).
EOF
ask 'Press Return to open the page... ' >/dev/null
open "https://account.apple.com/account/manage" || true
"$BIN" set-password icloud </dev/tty

# 5. test
say "Testing a read-only login"
"$BIN" check || fail "the check failed. Fix the problem above and run this installer again."

# 6. connect Claude
connected=""
if command -v claude >/dev/null 2>&1; then
  claude mcp remove --scope user "$SERVER" >/dev/null 2>&1 || true
  claude mcp add --scope user "$SERVER" -- "$BIN" serve && connected="Claude Code"
fi
if [ -d "$(dirname "$DESKTOP_CONFIG")" ]; then
  PY="$(uv tool dir)/$NAME/bin/python"
  if "$PY" -I - "$DESKTOP_CONFIG" "$SERVER" "$BIN" <<'PYEOF'
import json, os, shutil, sys
path, name, binary = sys.argv[1:]
cfg = {}
if os.path.exists(path):
    shutil.copy2(path, path + ".bak")
    with open(path) as f:
        cfg = json.load(f)
cfg.setdefault("mcpServers", {})[name] = {"command": binary, "args": ["serve"]}
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
PYEOF
  then
    connected="${connected:+$connected and }Claude Desktop (restart it to load)"
  else
    echo "Could not update $DESKTOP_CONFIG (left unchanged). See the README to add it by hand."
  fi
fi

# 7. first sync
say "Done installing"
if [ -n "$connected" ]; then
  echo "Connected to: $connected."
else
  echo "No Claude app found. To connect later: $BIN serve (see the README)."
fi
cat <<'EOF'

Now running the first sync. Newest mail comes first, so recent mail is searchable
within minutes. A big mailbox can take hours. You can press Ctrl-C at any time; it
picks up where it left off, and it also keeps syncing whenever Claude searches.
EOF
"$BIN" sync </dev/tty || true
