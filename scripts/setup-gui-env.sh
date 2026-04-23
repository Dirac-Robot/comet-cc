#!/usr/bin/env bash
# Wire CoMeT-CC into GUI apps (VSCode, JetBrains, Cursor, etc.) so their
# `claude` subprocess routes through the proxy without any per-launch
# gymnastics.
#
# Usage:
#   scripts/setup-gui-env.sh install            # current boot session only
#   scripts/setup-gui-env.sh install --persist  # + LaunchAgent for boot + daemon autostart
#   scripts/setup-gui-env.sh uninstall
#   scripts/setup-gui-env.sh status
#
# After `install`, quit + relaunch VSCode (and any other GUI app you want
# routed through the proxy) so they pick up the new environment.
set -euo pipefail

CMD="${1:-status}"
FLAG="${2:-}"

OS="$(uname)"
CA_PATH="${COMET_CC_HOME:-$HOME/.comet-cc}/certs/ca.crt"
PROXY_HOST="${COMET_CC_PROXY_HOST:-127.0.0.1}"
PROXY_PORT="${COMET_CC_PROXY_PORT:-8443}"
BASE_URL="https://${PROXY_HOST}:${PROXY_PORT}"

PLIST_LABEL="io.cometcc.daemon"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

ZSHENV="$HOME/.zshenv"
PROFILE="$HOME/.profile"
MARK_BEGIN="# >>> comet-cc env (managed — do not edit) >>>"
MARK_END="# <<< comet-cc env (managed) <<<"

_need_comet_cc() {
  command -v comet-cc >/dev/null 2>&1 || {
    echo "error: comet-cc not on PATH. Run \`pip install -e .\` first." >&2
    exit 1
  }
}

_shell_snippet() {
  cat <<EOF
${MARK_BEGIN}
export ANTHROPIC_BASE_URL="${BASE_URL}"
export NODE_EXTRA_CA_CERTS="${CA_PATH}"
${MARK_END}
EOF
}

_add_to_file() {
  local file="$1"
  touch "$file"
  if grep -qF "$MARK_BEGIN" "$file"; then
    # Replace existing block
    local tmp; tmp="$(mktemp)"
    awk -v beg="$MARK_BEGIN" -v end="$MARK_END" '
      $0 == beg { skip=1; next }
      $0 == end { skip=0; next }
      !skip
    ' "$file" > "$tmp"
    mv "$tmp" "$file"
  fi
  _shell_snippet >> "$file"
  echo "  → updated $file"
}

_remove_from_file() {
  local file="$1"
  [ -f "$file" ] || return 0
  if grep -qF "$MARK_BEGIN" "$file"; then
    local tmp; tmp="$(mktemp)"
    awk -v beg="$MARK_BEGIN" -v end="$MARK_END" '
      $0 == beg { skip=1; next }
      $0 == end { skip=0; next }
      !skip
    ' "$file" > "$tmp"
    mv "$tmp" "$file"
    echo "  → cleaned $file"
  fi
}

_install_launchagent() {
  local comet_bin; comet_bin="$(command -v comet-cc)"
  mkdir -p "$(dirname "$PLIST_PATH")"
  cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${PLIST_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${comet_bin}</string>
    <string>daemon</string>
    <string>start</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>ANTHROPIC_BASE_URL</key><string>${BASE_URL}</string>
    <key>NODE_EXTRA_CA_CERTS</key><string>${CA_PATH}</string>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>StandardOutPath</key><string>${HOME}/.comet-cc/logs/launchagent.out</string>
  <key>StandardErrorPath</key><string>${HOME}/.comet-cc/logs/launchagent.err</string>
</dict>
</plist>
EOF
  mkdir -p "$HOME/.comet-cc/logs"
  launchctl unload "$PLIST_PATH" 2>/dev/null || true
  launchctl load "$PLIST_PATH"
  echo "  → LaunchAgent loaded: ${PLIST_PATH}"
  echo "     (starts the daemon on login; ANTHROPIC_BASE_URL + NODE_EXTRA_CA_CERTS"
  echo "      available to every process launched via launchd)"
}

_uninstall_launchagent() {
  if [ -f "$PLIST_PATH" ]; then
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    echo "  → removed LaunchAgent"
  fi
}

cmd_install() {
  _need_comet_cc
  comet-cc install >/dev/null
  comet-cc daemon start >/dev/null 2>&1 || true

  if [ "$OS" = "Darwin" ]; then
    echo "macOS — current boot session:"
    launchctl setenv ANTHROPIC_BASE_URL "$BASE_URL"
    launchctl setenv NODE_EXTRA_CA_CERTS "$CA_PATH"
    echo "  → launchctl setenv set (visible to GUI apps until reboot)"

    echo
    echo "Shell profile (future terminals):"
    _add_to_file "$ZSHENV"

    if [ "$FLAG" = "--persist" ]; then
      echo
      echo "LaunchAgent (persistent across reboots + autostarts daemon):"
      _install_launchagent
    else
      echo
      echo "  (tip: re-run with --persist to install a LaunchAgent so"
      echo "   these vars survive reboots and the daemon autostarts at login)"
    fi
  else
    echo "Linux / other — shell profile:"
    _add_to_file "$PROFILE"
    echo
    echo "Log out + back in so your display manager re-reads \$HOME/.profile"
    echo "and exposes the env to GUI apps."
  fi

  echo
  echo "Restart your GUI editor (VSCode / JetBrains / Cursor …) to pick"
  echo "up the new environment. Verify with:"
  echo "   scripts/setup-gui-env.sh status"
}

cmd_uninstall() {
  if [ "$OS" = "Darwin" ]; then
    launchctl unsetenv ANTHROPIC_BASE_URL 2>/dev/null || true
    launchctl unsetenv NODE_EXTRA_CA_CERTS 2>/dev/null || true
    _remove_from_file "$ZSHENV"
    _uninstall_launchagent
  else
    _remove_from_file "$PROFILE"
  fi
  echo
  echo "Env vars cleared. Restart your GUI editor to drop the proxy route."
  echo "(The comet-cc home dir + store are left intact — delete manually if you want a full wipe.)"
}

cmd_status() {
  echo "Settings:"
  echo "  ANTHROPIC_BASE_URL target : ${BASE_URL}"
  echo "  NODE_EXTRA_CA_CERTS path  : ${CA_PATH}"
  [ -f "$CA_PATH" ] && echo "  CA exists                 : yes" || echo "  CA exists                 : NO — run \`comet-cc install\`"

  if [ "$OS" = "Darwin" ]; then
    echo
    echo "launchctl (current boot session):"
    for v in ANTHROPIC_BASE_URL NODE_EXTRA_CA_CERTS; do
      local val; val="$(launchctl getenv "$v" || true)"
      if [ -n "$val" ]; then echo "  ✓ $v=$val"; else echo "  ✗ $v not set"; fi
    done

    echo
    echo "LaunchAgent:"
    if [ -f "$PLIST_PATH" ]; then
      echo "  ✓ installed at $PLIST_PATH"
      launchctl list | grep -F "$PLIST_LABEL" || true
    else
      echo "  ✗ not installed (run with \`install --persist\` to enable)"
    fi
  fi

  echo
  echo "Shell profile marker:"
  for f in "$ZSHENV" "$PROFILE"; do
    [ -f "$f" ] || continue
    if grep -qF "$MARK_BEGIN" "$f"; then echo "  ✓ present in $f"; fi
  done

  echo
  echo "Daemon:"
  comet-cc daemon status 2>&1 | sed 's/^/  /' || true
}

case "$CMD" in
  install)   cmd_install ;;
  uninstall) cmd_uninstall ;;
  status)    cmd_status ;;
  *) echo "usage: $0 {install [--persist]|uninstall|status}" >&2; exit 2 ;;
esac
