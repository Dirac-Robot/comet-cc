#!/usr/bin/env bash
# Manual smoke test — runs 4 turns through the proxy, prints trim/sensor
# activity + the stored node. Leaves the daemon running afterwards.
#
#   scripts/demo.sh                                  # uses sonnet
#   MODEL=haiku scripts/demo.sh                      # cheaper
#   COMET_CC_MAX_L1=5 scripts/demo.sh                # force compact within 4 turns
set -euo pipefail

MODEL="${MODEL:-sonnet}"
CWD="${DEMO_CWD:-/tmp/comet-cc-demo}"
mkdir -p "$CWD"
cd "$CWD"

# 1. Ensure daemon + proxy are up
if ! comet-cc daemon status >/dev/null 2>&1; then
  echo "→ starting daemon..."
  comet-cc daemon start
fi

LOG="${COMET_CC_HOME:-$HOME/.comet-cc}/logs/daemon.log"
LOG_MARK=$(wc -l < "$LOG" 2>/dev/null || echo 0)

jq_result() { python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('result','')[:180])"; }
jq_sid()    { python3 -c "import json,sys; d=json.load(sys.stdin); print(d['session_id'])"; }

echo
echo "=== Turn 1 (new session) ==="
OUT=$(comet-cc run claude -p "Remember three fruits exactly: MANGO, LYCHEE, FIG. Reply with OK only." \
      --output-format json --model "$MODEL" < /dev/null)
SID=$(echo "$OUT" | jq_sid)
echo "session=$SID"
echo "< $(echo "$OUT" | jq_result)"

for PROMPT in \
  "Add DATE to that list. Reply with OK only." \
  "Also add KIWI. Reply OK." \
  "List every fruit on the list in order."; do
  echo
  echo "=== Turn (resume ${SID:0:8}) ==="
  echo "> $PROMPT"
  comet-cc run claude --resume "$SID" -p "$PROMPT" \
        --output-format json --model "$MODEL" < /dev/null | \
  tee /tmp/comet-cc-demo.last | jq_result | sed 's/^/< /'
  sleep 2
done

echo
echo "=== Waiting for compact worker (up to 90s) ==="
# compacter may still be running in the background after the last turn;
# poll queue_depth via the RPC so the demo can show the saved node.
for _ in $(seq 1 30); do
  Q=$(comet-cc daemon status 2>/dev/null || true)
  PY=${PY:-python3}
  DEPTH=$($PY -c "
from comet_cc import client
r = client.queue_depth(timeout=2.0) or {}
print(f\"{r.get('queue_size', 0)} {r.get('active', 0)}\")" 2>/dev/null || echo "? ?")
  echo "  queue=${DEPTH%% *} active=${DEPTH##* }"
  [ "$DEPTH" = "0 0" ] && break
  sleep 3
done

echo
echo "=== Proxy activity (new log lines) ==="
tail -n +"$((LOG_MARK+1))" "$LOG" | \
  grep -E "trim\[|sensor\[|compact\[" || echo "(no trim events yet — sensor may still be deliberating)"

echo
echo "=== Stored nodes for this session ==="
comet-cc list-session "$SID" || true

echo
echo "Tip: rerun with COMET_CC_MAX_L1=5 to force the overflow path"
echo "     (needs daemon restart: comet-cc daemon stop && COMET_CC_MAX_L1=5 comet-cc daemon start)"
