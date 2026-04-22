#!/usr/bin/env bash
# Stand up an isolated Claude Code directory with CoMeT-CC hooks + skill
# wired only for that project — leaves ~/.claude/settings.json alone.

set -euo pipefail

TEST_DIR="${1:-/tmp/cometcc-test}"
PY="${COMET_CC_PYTHON:-/Users/vanta/miniconda3/bin/python}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if ! "$PY" -c 'import comet_cc' 2>/dev/null; then
  echo "error: comet_cc not importable from $PY"
  echo "  hint: cd $REPO_ROOT && $PY -m pip install -e ."
  exit 1
fi

mkdir -p "$TEST_DIR/.claude/skills"

cat > "$TEST_DIR/.claude/settings.json" <<EOF
{
  "hooks": {
    "SessionStart": [{"matcher": "*", "hooks": [{"type": "command", "command": "$PY -m comet_cc.hooks.session_start"}]}],
    "UserPromptSubmit": [{"matcher": "*", "hooks": [{"type": "command", "command": "$PY -m comet_cc.hooks.user_prompt"}]}],
    "Stop": [{"matcher": "*", "hooks": [{"type": "command", "command": "$PY -m comet_cc.hooks.stop"}]}],
    "PreCompact": [{"matcher": "*", "hooks": [{"type": "command", "command": "$PY -m comet_cc.hooks.pre_compact"}]}]
  }
}
EOF

rm -rf "$TEST_DIR/.claude/skills/comet-cc-memory"
cp -r "$REPO_ROOT/skills/comet-cc-memory" "$TEST_DIR/.claude/skills/"

echo "Throwaway CC project ready: $TEST_DIR"
echo
echo "Next:"
echo "  cd $TEST_DIR"
echo "  claude"
echo
echo "Observe from another terminal:"
echo "  tail -F ~/.comet-cc/logs/daemon.log ~/.comet-cc/logs/hook.log"
echo "  sqlite3 ~/.comet-cc/store.sqlite 'SELECT node_id, recall_mode, importance, summary FROM nodes'"
echo
echo "When done:"
echo "  $PY -m comet_cc.cli daemon stop"
echo "  rm -rf $TEST_DIR ~/.comet-cc"
