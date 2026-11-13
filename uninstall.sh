#!/bin/bash
# Removes readonly-safe-mail-mcp from this Mac: everything install.sh could have added.
#
#   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/pkellner/readonly-safe-mail-mcp/main/uninstall.sh)"
#
# It first shows what it found and asks once before removing anything:
#   - the connections in Claude Code, Claude Desktop and Codex (other servers are kept)
#   - the saved password(s) in your login Keychain
#   - the settings folder  ~/.config/readonly-safe-mail-mcp
#   - the downloaded mail  ~/Library/Application Support/readonly-safe-mail-mcp
#     (the same folder holds the contacts and calendar index and its lock, if you turned
#     them on; they go with it)
#   - Claude Desktop's log file for this server
#   - the tool itself (uv tool uninstall) and its cached download
#   - uv, only if you agree (asked separately; it is never removed if other tools use it)
# Your mail, contacts and calendar on iCloud are not touched. Safe to run again; it skips what is already gone.
set -uo pipefail

NAME="readonly-safe-mail-mcp"
SERVER="readonly-safe-mail"
RAW="https://raw.githubusercontent.com/pkellner/readonly-safe-mail-mcp/main"
CONFIG_DIR="${RSMM_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/$NAME}"
DATA_DIR="${RSMM_DATA_DIR:-$HOME/Library/Application Support/$NAME}"
ACCOUNTS="$CONFIG_DIR/accounts.yaml"
UV_MARKER="$CONFIG_DIR/.installer-installed-uv"
DESKTOP_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
DESKTOP_LOG="$HOME/Library/Logs/Claude/mcp-server-$SERVER.log"
CLAUDE_JSON="$HOME/.claude.json"
CODEX_CONFIG="${CODEX_HOME:-$HOME/.codex}/config.toml"
# shellcheck disable=SC2089,SC2090  # display text, never executed
UNINSTALL_CMD="/bin/bash -c \"\$(curl -fsSL $RAW/uninstall.sh)\""

B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
say()  { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
note() { printf '  • %s\n' "$*"; }
warn() { printf '  %s! %s%s\n' "$Y" "$*" "$N"; }
problems=()
problem() { printf '  %s✗ %s%s\n' "$R" "$1" "$N"; problems+=("$1${2:+ -> $2}"); }
# shellcheck disable=SC2090
trap 'printf "\n\n%sStopped.%s Some things may already be removed. Run the uninstall command again to finish:\n  %s\n" "$Y" "$N" "$UNINSTALL_CMD"; exit 130' INT

ask() {
  if ! read -r -p "$1" REPLY </dev/tty; then printf '\nCancelled. Nothing more was removed.\n'; exit 1; fi
  REPLY="${REPLY#"${REPLY%%[![:space:]]*}"}"; REPLY="${REPLY%"${REPLY##*[![:space:]]}"}"
}
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

[ "$(uname -s)" = "Darwin" ] || { echo "This uninstaller is for macOS only."; exit 1; }
{ : </dev/tty; } 2>/dev/null || { echo "Run this in a Terminal window (Applications -> Utilities -> Terminal)."; exit 1; }

export PATH="$HOME/.local/bin:$PATH"
UV="$(command -v uv 2>/dev/null || true)"
if [ -n "$UV" ]; then
  TOOL_DIR="$(uv tool dir 2>/dev/null)/$NAME"
  BIN="$(uv tool dir --bin 2>/dev/null)/$NAME"
else
  TOOL_DIR="$HOME/.local/share/uv/tools/$NAME"
  BIN="$HOME/.local/bin/$NAME"
fi
PY="$TOOL_DIR/bin/python"
[ -x "$PY" ] || PY=""

# A Python that can edit JSON/TOML: the tool's own, else one from uv.
run_py() {
  if [ -n "$PY" ]; then "$PY" -I - "$@"
  elif [ -n "$UV" ]; then uv run --quiet --no-project --python 3.13 python -I - "$@"
  else return 9
  fi
}

# Drop our server (by name, or any entry that runs this tool) from the config and its .bak.
desktop_edit() {
  run_py "$DESKTOP_CONFIG" "$SERVER" "$NAME" <<'PYEOF'
import json, os, sys
path, name, tool = sys.argv[1:]
for p in (path, path + ".bak"):
    if not os.path.exists(p):
        continue
    with open(p) as f:
        cfg = json.load(f)
    servers = cfg.get("mcpServers") or {}
    drop = [k for k, v in servers.items() if k == name or tool in json.dumps(v)]
    if drop:
        for k in drop:
            del servers[k]
        with open(p, "w") as f:
            json.dump(cfg, f, indent=2)
PYEOF
}
# Remove each [mcp_servers.X] table (and its sub-tables) whose body runs this tool.
codex_edit() {
  run_py "$CODEX_CONFIG" "$NAME" <<'PYEOF'
import re, shutil, sys
path, tool = sys.argv[1:]
lines = open(path).read().splitlines(keepends=True)
header = re.compile(r"^\s*\[\s*mcp_servers\.([^\].]+|\"[^\"]+\")(\.[^\]]*)?\s*\]")
sections, cur = [], {"name": None, "lines": []}
for line in lines:
    m = re.match(r"^\s*\[", line) and header.match(line)
    if re.match(r"^\s*\[", line):
        sections.append(cur)
        cur = {"name": m.group(1) if m else None, "lines": []}
    cur["lines"].append(line)
sections.append(cur)
bad = {s["name"] for s in sections if s["name"] and any(tool in l for l in s["lines"])}
if bad:
    shutil.copy2(path, path + ".bak-before-uninstall")
    with open(path, "w") as f:
        f.writelines(l for s in sections if s["name"] not in bad for l in s["lines"])
PYEOF
}

# ---------------------------------------------------------------- what is here?
mentions() { [ -f "$1" ] && grep -q -e "$NAME" -e "\"$SERVER\"" "$1" 2>/dev/null; }
has_keychain() { security find-generic-password -s "$NAME" >/dev/null 2>&1; }
# What the data folder holds: the contacts and calendar index (dav-*.sqlite) only if they were on.
data_what="mail"
compgen -G "$DATA_DIR/dav-*.sqlite*" >/dev/null && data_what="mail, contacts and calendar"
has_tool=""; { [ -d "$TOOL_DIR" ] || [ -e "$BIN" ] || [ -L "$BIN" ]; } && has_tool=1
found=()
[ -n "$has_tool" ] && found+=("The tool: $TOOL_DIR")
[ -d "$CONFIG_DIR" ] && found+=("Settings: $CONFIG_DIR")
[ -d "$DATA_DIR" ] && found+=("Downloaded $data_what ($(du -sh "$DATA_DIR" 2>/dev/null | cut -f1 | tr -d ' ')): $DATA_DIR")
{ has_keychain || [ -d "$CONFIG_DIR/credentials" ]; } && found+=("Saved password in your Keychain")
HAS_CLAUDE=""; command -v claude >/dev/null 2>&1 && HAS_CLAUDE=1
in_claude_code() { [ -n "$HAS_CLAUDE" ] && (cd "$HOME" && claude mcp get "$SERVER" >/dev/null 2>&1); }
cc=""; in_claude_code && cc=1
[ -n "$cc" ] && found+=("Connection in Claude Code")
mentions "$DESKTOP_CONFIG" && found+=("Connection in Claude Desktop")
mentions "$CODEX_CONFIG" && found+=("Connection in Codex")
[ -f "$DESKTOP_LOG" ] && found+=("Claude Desktop log for this server: $DESKTOP_LOG")

printf '\n%sreadonly-safe-mail-mcp uninstaller%s\n' "$B" "$N"
if [ ${#found[@]} -eq 0 ]; then
  printf '\nNothing to remove: readonly-safe-mail-mcp is not installed on this Mac.\n'
  printf 'If you made an app-specific password for it, you can revoke it at https://account.apple.com\n'
  printf '(Sign-In and Security -> App-Specific Passwords).\n'
  exit 0
fi
echo
echo "This will permanently remove from this Mac:"
for f in "${found[@]}"; do note "$f"; done
echo
echo "Your $data_what on iCloud is not touched; only the local copy is deleted."
if ! yesno "Remove all of this?" n; then
  printf '\nCancelled. Nothing was removed.\n'; exit 0
fi

# ---------------------------------------------------------------- 1. stop running copies
say "Stopping running copies"
# A running server looks like: <python> /Users/you/.local/bin/readonly-safe-mail-mcp serve
procs="$BIN( |$)|$TOOL_DIR/"
if pgrep -f "$procs" >/dev/null 2>&1; then
  pkill -f "$procs" 2>/dev/null; sleep 2
  pkill -9 -f "$procs" 2>/dev/null
  ok "Stopped the running mail server/sync (Claude will show it as disconnected)"
else
  note "None running"
fi

# ---------------------------------------------------------------- 2. connections
say "Removing the connections to Claude and Codex"
if [ -n "$cc" ]; then
  for scope in user local; do
    (cd "$HOME" && claude mcp remove --scope "$scope" "$SERVER" >/dev/null 2>&1)
  done
  if in_claude_code; then
    problem "Claude Code: could not remove the \"$SERVER\" server" "run: claude mcp remove $SERVER"
  else
    ok "Claude Code: disconnected (start a new Claude Code session)"
  fi
else
  note "Claude Code: not connected"
fi
if grep -q "\"$SERVER\"" "$CLAUDE_JSON" 2>/dev/null; then
  note "Claude Code may still list \"$SERVER\" inside a specific project folder. If you added it"
  note "there yourself, open that folder in Terminal and run: claude mcp remove $SERVER"
fi

if mentions "$DESKTOP_CONFIG"; then
  out="$(desktop_edit 2>&1)"
  rc=$?
  if [ $rc -eq 0 ] && ! mentions "$DESKTOP_CONFIG"; then
    ok "Claude Desktop: disconnected (quit and reopen it if it is open)"
  else
    problem "Claude Desktop: could not edit $DESKTOP_CONFIG" \
      "open it in TextEdit and delete the \"$SERVER\" entry under mcpServers"
    [ -z "$out" ] || printf '      %s\n' "$out"
  fi
else
  note "Claude Desktop: not connected"
fi

if mentions "$CODEX_CONFIG"; then
  out="$(codex_edit 2>&1)"
  rc=$?
  if [ $rc -eq 0 ] && ! grep -q "$NAME" "$CODEX_CONFIG"; then
    ok "Codex: disconnected (backup: $CODEX_CONFIG.bak-before-uninstall; restart Codex)"
  else
    problem "Codex: could not edit $CODEX_CONFIG" \
      "open it and delete the [mcp_servers...] section that runs $NAME"
    [ -z "$out" ] || printf '      %s\n' "$out"
  fi
else
  note "Codex: not connected"
fi

# ---------------------------------------------------------------- 3. password
say "Removing the saved password"
if has_keychain || [ -d "$CONFIG_DIR/credentials" ]; then
  accts="icloud"
  if [ -n "$PY" ] && [ -f "$ACCOUNTS" ]; then
    accts="$("$PY" -I -c 'import sys, yaml; print(" ".join(yaml.safe_load(open(sys.argv[1]))["accounts"]))' \
      "$ACCOUNTS" 2>/dev/null) icloud"
  fi
  if [ -x "$BIN" ]; then
    for a in $accts; do "$BIN" delete-password "$a" >/dev/null 2>&1; done
  fi
  # Fallback (tool already gone, or an account not in the settings file).
  n=0
  while has_keychain && [ $n -lt 20 ]; do
    security delete-generic-password -s "$NAME" >/dev/null 2>&1 || break
    n=$((n + 1))
  done
  if has_keychain; then
    problem "Keychain: the password could not be removed" \
      "open Keychain Access, search for $NAME, and delete it"
  else
    ok "Removed from your Keychain"
  fi
else
  note "No saved password found"
fi

# ---------------------------------------------------------------- 4. files
say "Removing settings, downloaded mail and logs"
uv_marked=""; [ -f "$UV_MARKER" ] && uv_marked=1
for pair in "settings|$CONFIG_DIR" "downloaded $data_what|$DATA_DIR" "Claude Desktop log|$DESKTOP_LOG"; do
  label="${pair%%|*}"; path="${pair#*|}"
  if [ -e "$path" ]; then
    if rm -rf "$path"; then ok "Removed the $label ($path)"
    else problem "Could not remove the $label" "delete $path in Finder"; fi
  else
    note "No $label found"
  fi
done
rm -f "$HOME/Library/Logs/Claude/mcp-server-$SERVER".log.* 2>/dev/null

# ---------------------------------------------------------------- 5. the tool
say "Removing the tool"
if [ -n "$has_tool" ]; then
  if [ -n "$UV" ]; then
    uv tool uninstall "$NAME" >/dev/null 2>&1
    uv cache clean "$NAME" >/dev/null 2>&1
  fi
  rm -rf "$TOOL_DIR"; [ -L "$BIN" ] || [ -e "$BIN" ] && rm -f "$BIN"
  if [ -e "$TOOL_DIR" ] || [ -e "$BIN" ]; then
    problem "Could not remove the tool" "delete $TOOL_DIR and $BIN in Finder"
  else
    ok "Removed $NAME"
  fi
else
  note "Tool not installed"
fi

# ---------------------------------------------------------------- 6. uv (optional)
if [ -n "$UV" ]; then
  say "uv"
  others="$(uv tool list 2>/dev/null | grep -v '^-' | grep -v '^No tools' | grep -v "^$NAME " || true)"
  if [ "$UV" != "$HOME/.local/bin/uv" ]; then
    note "Keeping uv: it was installed some other way ($UV), not by this installer"
  elif [ -n "$others" ]; then
    note "Keeping uv: you use it for other tools too ($(echo "$others" | cut -d' ' -f1 | tr '\n' ' '))"
  else
    if [ -n "$uv_marked" ]; then
      q="uv was installed by this installer. Remove it too?"; def=y
    else
      q="uv is installed and nothing else uses it. It may have come from this installer or from you. Remove it?"; def=n
    fi
    if yesno "$q" "$def"; then
      pydir="$(uv python dir 2>/dev/null)"
      uv cache clean >/dev/null 2>&1
      for f in "$HOME"/.local/bin/python*; do
        [ -L "$f" ] && [ -n "$pydir" ] && case "$(readlink "$f")" in "$pydir"*) rm -f "$f" ;; esac
      done
      [ -n "$pydir" ] && rm -rf "$pydir"
      rm -rf "$(uv tool dir 2>/dev/null)"
      rm -f "$HOME/.local/bin/uv" "$HOME/.local/bin/uvx" "$HOME/.config/uv/uv-receipt.json"
      rmdir "$HOME/.config/uv" 2>/dev/null
      rmdir "$HOME/.local/share/uv" 2>/dev/null
      if [ -e "$HOME/.local/bin/uv" ]; then problem "Could not remove uv" "delete ~/.local/bin/uv"
      else ok "Removed uv, its Python downloads and its cache"; fi
    else
      note "Keeping uv"
    fi
  fi
fi

# ---------------------------------------------------------------- summary
echo
if [ ${#problems[@]} -eq 0 ]; then
  printf '%sDone. readonly-safe-mail-mcp has been removed from this Mac.%s\n' "$B" "$N"
else
  printf '%sAlmost done. These items need a hand:%s\n' "$Y" "$N"
  for p in "${problems[@]}"; do printf '  - %s\n' "$p"; done
  printf 'Or run the uninstall command again:\n  %s\n' "$UNINSTALL_CMD"
fi
cat <<EOF

${B}One step only you can do:${N} revoke the app-specific password at Apple.
  Go to https://account.apple.com -> Sign-In and Security -> App-Specific Passwords,
  and remove the one you made for this tool (for example "mail mcp").
If Claude Desktop or Codex is open, quit and reopen it.
EOF
