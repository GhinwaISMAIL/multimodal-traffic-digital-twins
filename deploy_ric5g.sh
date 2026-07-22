#!/usr/bin/env bash
# Multi-node MGEN runner for the oai-5g-ric POWDER profile.
# Runs from the workstation. Drives generated .mgn scripts across core + cells.
#
#   ./deploy_ric5g.sh <run_dir> [duration]
#
# Phases: check -> resolve IPs -> rewrite DL -> snapshot RNTI -> push ->
#         teardown -> arm receivers -> start senders -> wait -> collect
set -euo pipefail

RUN_DIR="${1:?usage: deploy_ric5g.sh <run_dir> [duration]}"
DURATION="${2:-600}"

CORE_HOST="${CORE_HOST:-ghinwa@pc798.emulab.net}"
UES_PER_CELL="${UES_PER_CELL:-12}"
NUM_CELLS="${NUM_CELLS:-2}"
NB_ID_START="${NB_ID_START:-3584}"
HOST_LOG_DIR=/local/logs/mgen
REMOTE_BIN="${REMOTE_BIN:-/local/repository/bin}"
DN_CONTAINER="${DN_CONTAINER:-ric5g-oai-ext-dn}"
RUN_ID="mgen-$(date +%Y%m%d-%H%M%S)"
XAPP_FAILED=0
MGEN_FAILED=0
CHANNEL_FAILED=0
RUNNER_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CHANNEL_SCHEDULE="$RUN_DIR/channel_schedule.json"

[[ "$NUM_CELLS" =~ ^[0-9]+$ ]] && [ "$NUM_CELLS" -ge 1 ] && [ "$NUM_CELLS" -le 3 ] || {
    echo "NUM_CELLS must be between 1 and 3" >&2
    exit 1
}

CELL_HOSTS=()
for cell_index in $(seq 1 "$NUM_CELLS"); do
    variable="CELL${cell_index}_HOST"
    host="${!variable:-}"
    [ -n "$host" ] || {
        echo "$variable is required when NUM_CELLS=$NUM_CELLS" >&2
        exit 1
    }
    CELL_HOSTS+=("$host")
done

SCRIPTS="$RUN_DIR/mgen_scripts"
LOGS="$RUN_DIR/logs"
mkdir -p "$LOGS"
[ -d "$SCRIPTS" ] || { echo "no mgen_scripts in $RUN_DIR" >&2; exit 1; }

cell_of()  { echo $(( ($1 - 1) / UES_PER_CELL + 1 )); }
ue_of()    { echo $(( ($1 - 1) % UES_PER_CELL + 1 )); }
host_of()  { echo "${CELL_HOSTS[$(( $1 - 1 ))]}"; }
nb_of()    { echo $(( NB_ID_START + $1 - 1 )); }

UE_LIST=$(ls "$SCRIPTS" | sed -n 's/^ue\([0-9]\{1,\}\)_ul_tx\.mgn$/\1/p' | sort -n)
[ -n "$UE_LIST" ] || { echo "no ue*_ul_tx.mgn found" >&2; exit 1; }
UE_COUNT=$(echo "$UE_LIST" | wc -l | tr -d ' ')
CAPACITY=$((NUM_CELLS * UES_PER_CELL))
[ "$UE_COUNT" -le "$CAPACITY" ] || {
    echo "$UE_COUNT generated UEs exceed $NUM_CELLS cell(s) x $UES_PER_CELL UE(s) capacity" >&2
    exit 1
}
echo "run_id=$RUN_ID  duration=${DURATION}s  cells=$NUM_CELLS  ues=$UE_COUNT"

echo "== 1/9 check core =="
ssh "$CORE_HOST" "sudo env MGEN_DN_CONTAINER=$DN_CONTAINER bash $REMOTE_BIN/mgen-core.sh check"

echo "== 2/9 check cells and resolve live PDU IPs =="
: > "$LOGS/ue_ips.txt"
for n in $UE_LIST; do
    c=$(cell_of "$n"); u=$(ue_of "$n"); h=$(host_of "$c")
    ip=$(ssh "$h" "sudo docker exec ric5g-ue-cell$c-$u ip -4 -o addr show oaitun_ue1" \
         | awk '$4 ~ /^12\.1\.1\./ {sub(/\/.*/,"",$4); print $4; exit}')
    [ -n "$ip" ] || { echo "ue$n (cell$c/ue$u) has no PDU address" >&2; exit 1; }
    echo "ue$n $c $u $ip" >> "$LOGS/ue_ips.txt"
    ssh "$h" "sudo bash $REMOTE_BIN/mgen-cell.sh check $c $UES_PER_CELL $u" >/dev/null
done
column -t "$LOGS/ue_ips.txt" | sed 's/^/  /'

echo "== 2b/9 capture node clock anchors =="
ANCHORS="$LOGS/.anchors"
: > "$ANCHORS"

anchor_node() {   # name host
    local name=$1 host=$2 before after raw
    before=$(date +%s.%N)
    # MGEN renders SEND/RECV timestamps in UTC regardless of the host's civil
    # timezone, so anchor the seconds-of-day component in UTC as well.
    raw=$(ssh "$host" "date -u +'%s.%N|%H:%M:%S.%N'")
    after=$(date +%s.%N)
    printf '%s\t%s\t%s\t%s\t%s\n' "$name" "$host" "$before" "$raw" "$after" >> "$ANCHORS"
    echo "  $name $raw"
}

anchor_node core "$CORE_HOST"
for _c in $(seq 1 "$NUM_CELLS"); do
    anchor_node "cell$_c" "$(host_of "$_c")"
done


echo "== 3/9 rewrite DN downlink with live IPs =="
cp "$SCRIPTS/dn_dl_tx.mgn" "$LOGS/dn_dl_tx.mgn"
if [ -f "$SCRIPTS/manifest.csv" ]; then
    # Rewrite all destinations as one transaction.  Sequential substitutions
    # are unsafe when a live address is another UE's designed address: e.g.
    # .1 -> .3 followed by .3 -> .4 also rewrites the first UE a second time.
    python3 - \
      "$SCRIPTS/manifest.csv" \
      "$LOGS/ue_ips.txt" \
      "$LOGS/dn_dl_tx.mgn" <<'PYEOF'
import csv
from pathlib import Path
import sys

manifest_path, live_path, script_path = map(Path, sys.argv[1:])

with manifest_path.open(newline="") as stream:
    manifest = {
        row["ue_name"]: row["ue_ip"]
        for row in csv.DictReader(stream)
    }

live = {}
for line in live_path.read_text().splitlines():
    fields = line.split()
    if len(fields) != 4:
        raise SystemExit(f"invalid live UE mapping: {line!r}")
    name, _cell, _ue, address = fields
    live[name] = address

text = script_path.read_text()
tokens = {}

# First replace every designed address with a non-IP token.  Only after all
# designed addresses are gone do we insert the live addresses.
for name, address in live.items():
    designed = manifest.get(name)
    if not designed:
        raise SystemExit(f"manifest has no designed address for {name}")
    needle = f"DST {designed}/"
    if needle not in text:
        raise SystemExit(
            f"DN script has no downlink destination for {name} ({designed})"
        )
    token = f"__RIC5G_DST_{name}__"
    text = text.replace(needle, f"DST {token}/")
    tokens[token] = address
    if designed != address:
        print(f"  {name}: {designed} -> {address}")

for token, address in tokens.items():
    text = text.replace(f"DST {token}/", f"DST {address}/")

if "__RIC5G_DST_" in text:
    raise SystemExit("unresolved DN destination token after live-IP rewrite")

for name, address in live.items():
    if f"DST {address}/" not in text:
        raise SystemExit(f"rewritten DN script has no destination for {name}")

script_path.write_text(text)
PYEOF
fi

echo "== 4/9 snapshot UE -> RNTI =="
: > "$LOGS/rnti_map.csv"
echo "ue,cell,ue_index,nb_id,rnti,pdu_ip" >> "$LOGS/rnti_map.csv"
RNTI_MISSING=0
for n in $UE_LIST; do
    c=$(cell_of "$n"); u=$(ue_of "$n"); h=$(host_of "$c")
    ip=$(awk -v k="ue$n" '$1==k{print $4}' "$LOGS/ue_ips.txt")
    hex=$(ssh "$h" "sudo docker logs ric5g-ue-cell$c-$u 2>&1 | grep -oE 'RNTI [0-9a-fA-F]{4}' | tail -1 | cut -d' ' -f2" || true)
    if [ -z "$hex" ]; then
        echo "  WARN: ue$n has no RNTI in its log"
        RNTI_MISSING=$((RNTI_MISSING + 1))
        continue
    fi
    echo "ue$n,$c,$u,$(nb_of "$c"),$((16#$hex)),$ip" >> "$LOGS/rnti_map.csv"
done
sed 's/^/  /' "$LOGS/rnti_map.csv"
if [ "${XAPP:-0}" = 1 ] && [ "$RNTI_MISSING" -ne 0 ]; then
    echo "missing RNTI mappings for $RNTI_MISSING UE(s); refusing an unmappable PRB run" >&2
    exit 1
fi
if [ "${XAPP:-0}" = 1 ]; then
    RNTI_DUPLICATES=$(tail -n +2 "$LOGS/rnti_map.csv" | cut -d, -f4,5 | sort | uniq -d)
    if [ -n "$RNTI_DUPLICATES" ]; then
        echo "duplicate (nb_id,rnti) mappings; refusing an ambiguous PRB run:" >&2
        echo "$RNTI_DUPLICATES" | sed 's/^/  /' >&2
        exit 1
    fi
fi

echo "== 5/9 push scripts =="
push() {  # host container file
    local h=$1 ctr=$2 f=$3 b
    b=$(basename "$f")
    scp -q "$f" "$h:/tmp/$b"
    ssh "$h" "sudo docker exec $ctr mkdir -p /logs/mgen && sudo docker cp /tmp/$b $ctr:/logs/mgen/$b"
}
push "$CORE_HOST" "$DN_CONTAINER" "$LOGS/dn_dl_tx.mgn"
push "$CORE_HOST" "$DN_CONTAINER" "$SCRIPTS/dn_ul_rx.mgn"
for n in $UE_LIST; do
    c=$(cell_of "$n"); u=$(ue_of "$n"); h=$(host_of "$c")
    push "$h" "ric5g-ue-cell$c-$u" "$SCRIPTS/ue${n}_ul_tx.mgn"
    push "$h" "ric5g-ue-cell$c-$u" "$SCRIPTS/ue${n}_dl_rx.mgn"
done

echo "== 6/9 teardown stale mgen =="
ssh "$CORE_HOST" "sudo docker exec $DN_CONTAINER pkill -9 mgen || true" >/dev/null 2>&1
for n in $UE_LIST; do
    c=$(cell_of "$n"); u=$(ue_of "$n"); h=$(host_of "$c")
    ssh "$h" "sudo docker exec ric5g-ue-cell$c-$u pkill -9 mgen || true" >/dev/null 2>&1 &
done
wait

echo "== 7/9 arm receivers =="
ssh "$CORE_HOST" "sudo env MGEN_DN_CONTAINER=$DN_CONTAINER bash $REMOTE_BIN/mgen-core.sh run-script $RUN_ID dn_ul_rx.mgn $((DURATION + 30)) rx"
for n in $UE_LIST; do
    c=$(cell_of "$n"); u=$(ue_of "$n"); h=$(host_of "$c")
    ssh "$h" "sudo bash $REMOTE_BIN/mgen-cell.sh run-script $RUN_ID $c $u ue${n}_dl_rx.mgn $((DURATION + 30)) rx" &
done
wait
sleep 5

CHANNEL_ARGS=()
CHANNEL_ACTIVE=0
if [ -f "$CHANNEL_SCHEDULE" ]; then
    CHANNEL_ACTIVE=$(python3 - "$CHANNEL_SCHEDULE" <<'PYEOF'
import json, sys
try:
    value = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    raise SystemExit("invalid channel_schedule.json")
print(1 if value.get("enabled", True) else 0)
PYEOF
)
fi
if [ "$CHANNEL_ACTIVE" -eq 1 ]; then
    echo "== 7a/9 verify runtime channel control =="
    for channel_cell in $(seq 1 "$NUM_CELLS"); do
        CHANNEL_ARGS+=(--cell-host "$channel_cell=$(host_of "$channel_cell")")
    done
    python3 "$RUNNER_DIR/channel_schedule.py" check \
        --schedule "$CHANNEL_SCHEDULE" \
        --ue-map "$LOGS/ue_ips.txt" \
        --remote-bin "$REMOTE_BIN" \
        --duration "$DURATION" \
        "${CHANNEL_ARGS[@]}"
fi

if [ "${XAPP:-0}" = 1 ]; then
    echo "== 7b/9 verify RIC is ready =="
    WANT_SUBS="${XAPP_SUBS:-8}"
    DELAY="${XAPP_DELAY:-90}"
    WINDOW="${XAPP_WINDOW:-60}"
    if [ $((DELAY + WINDOW)) -gt "$DURATION" ]; then
        echo "xApp window ends at $((DELAY + WINDOW))s, after the ${DURATION}s traffic run" >&2
        exit 1
    fi
    ASSOC=$(ssh "$CORE_HOST" "sudo awk 'NR>1 && \$12==36421 {c++} END {print c+0}' /proc/net/sctp/assocs")
    STALE=$(ssh "$CORE_HOST" "sudo awk 'NR>1 && (\$12==36422 || \$13==36422) {c++} END {print c+0}' /proc/net/sctp/assocs")
    echo "  e2_associations=$ASSOC  stale_e42=$STALE"
    [ "$ASSOC" -eq "$NUM_CELLS" ] || { echo "expected $NUM_CELLS E2 associations, found $ASSOC" >&2; exit 1; }
    [ "$STALE" -eq 0 ] || { echo "stale E42 associations on the RIC - restart it before running" >&2; exit 1; }
    FREE_KB=$(ssh "$CORE_HOST" "df -Pk /tmp | awk 'NR==2 {print \$4}'")
    MIN_FREE_KB="${XAPP_MIN_FREE_KB:-1048576}"
    echo "  xapp_free_kb=$FREE_KB  required_kb=$MIN_FREE_KB"
    [ "$FREE_KB" -ge "$MIN_FREE_KB" ] || { echo "insufficient /tmp space for xApp SQLite" >&2; exit 1; }
fi

echo "== 8/9 start senders =="
SENDERS_START=$(ssh "$CORE_HOST" "date +%s.%N")   # core clock, not the workstation's
CHANNEL_START=$(date +%s.%N)
ssh "$CORE_HOST" "sudo env MGEN_DN_CONTAINER=$DN_CONTAINER bash $REMOTE_BIN/mgen-core.sh run-script $RUN_ID dn_dl_tx.mgn $DURATION tx" &
for n in $UE_LIST; do
    c=$(cell_of "$n"); u=$(ue_of "$n"); h=$(host_of "$c")
    ssh "$h" "sudo bash $REMOTE_BIN/mgen-cell.sh run-script $RUN_ID $c $u ue${n}_ul_tx.mgn $DURATION tx" &
done
echo "traffic running for ${DURATION}s"

if [ "$CHANNEL_ACTIVE" -eq 1 ]; then
    echo "== 8a/9 channel schedule =="
    (
        set +e
        python3 "$RUNNER_DIR/channel_schedule.py" run \
            --schedule "$CHANNEL_SCHEDULE" \
            --ue-map "$LOGS/ue_ips.txt" \
            --remote-bin "$REMOTE_BIN" \
            --duration "$DURATION" \
            --start-epoch "$CHANNEL_START" \
            --output "$LOGS/channel_state.json" \
            "${CHANNEL_ARGS[@]}"
        echo "$?" > "$LOGS/.channel_rc"
    ) &
fi

if [ "${XAPP:-0}" = 1 ]; then
    XAPP_DIR=/opt/oai-src/openair2/E2AP/flexric
    XAPP_BIN=./build/examples/xApp/c/monitor/xapp_gtp_mac_rlc_pdcp_moni
    XAPP_LOG="/local/logs/xapp-$RUN_ID.log"
    echo "== 8b/9 xApp window: +${DELAY}s for ${WINDOW}s =="
    sleep "$DELAY"
    XAPP_START=$(ssh "$CORE_HOST" "date +%s.%N")
    XAPP_PID=$(ssh -n "$CORE_HOST" "cd $XAPP_DIR; nohup stdbuf -oL -eL $XAPP_BIN > $XAPP_LOG 2>&1 </dev/null & printf '%s\\n' \$!" | tr -cd '0-9')
    if ! [[ "$XAPP_PID" =~ ^[0-9]+$ ]]; then
        echo "  ERROR: could not capture the xApp PID"
        XAPP_FAILED=1
    fi
    echo "  xapp_pid=$XAPP_PID"
    SUBS=0
    for _ in $(seq 1 20); do
        SUBS=$(ssh "$CORE_HOST" "grep -c 'Successfully subscribed' $XAPP_LOG 2>/dev/null || true" | head -1)
        [ -n "$SUBS" ] || SUBS=0
        [ "$SUBS" -ge "$WANT_SUBS" ] && break
        sleep 2
    done
    if [ "$SUBS" -ge "$WANT_SUBS" ]; then
        echo "  subscribed $SUBS/$WANT_SUBS, capturing ${WINDOW}s"
        sleep "$WINDOW"
    else
        echo "  ERROR: only $SUBS/$WANT_SUBS subscriptions"
        XAPP_FAILED=1
    fi
    ssh "$CORE_HOST" "kill -INT '$XAPP_PID' 2>/dev/null || true"
    for _ in $(seq 1 20); do
        ssh "$CORE_HOST" "kill -0 '$XAPP_PID' 2>/dev/null" || break
        sleep 2
    done
    if ssh "$CORE_HOST" "kill -0 '$XAPP_PID' 2>/dev/null"; then
        echo "  ERROR: xApp did not stop after SIGINT; escalating"
        XAPP_FAILED=1
        ssh "$CORE_HOST" "kill -TERM '$XAPP_PID' 2>/dev/null || true"
        sleep 2
        ssh "$CORE_HOST" "kill -KILL '$XAPP_PID' 2>/dev/null || true"
    fi
    DELS=$(ssh "$CORE_HOST" "grep -c 'SUBSCRIPTION DELETE RESPONSE rx' $XAPP_LOG 2>/dev/null || true" | head -1)
    [ -n "$DELS" ] || DELS=0
    echo "  delete responses: $DELS/$WANT_SUBS"
    [ "$DELS" -eq "$WANT_SUBS" ] || XAPP_FAILED=1
    XAPP_ERRORS=$(ssh "$CORE_HOST" "grep -ciE 'assert|aborted|timeout|pending event|connection lost|segmentation|SCTP_SEND_FAILED|(^|[^A-Za-z])ERROR:' $XAPP_LOG 2>/dev/null || true" | head -1)
    [ -n "$XAPP_ERRORS" ] || XAPP_ERRORS=0
    XAPP_SUCCESS=$(ssh "$CORE_HOST" "grep -c 'Test xApp run SUCCESSFULLY' $XAPP_LOG 2>/dev/null || true" | head -1)
    [ -n "$XAPP_SUCCESS" ] || XAPP_SUCCESS=0
    echo "  xapp_errors=$XAPP_ERRORS  clean_success=$XAPP_SUCCESS"
    [ "$XAPP_ERRORS" -eq 0 ] || XAPP_FAILED=1
    [ "$XAPP_SUCCESS" -ge 1 ] || XAPP_FAILED=1
    E42_AFTER=1
    for _ in $(seq 1 10); do
        E42_AFTER=$(ssh "$CORE_HOST" "sudo awk 'NR>1 && (\$12==36422 || \$13==36422) {c++} END {print c+0}' /proc/net/sctp/assocs")
        [ "$E42_AFTER" -eq 0 ] && break
        sleep 1
    done
    echo "  remaining_e42=$E42_AFTER"
    [ "$E42_AFTER" -eq 0 ] || XAPP_FAILED=1
    scp -q "$CORE_HOST:$XAPP_LOG" "$LOGS/xapp.log" || XAPP_FAILED=1
    XAPP_DB=$(ssh "$CORE_HOST" "sed -n 's/.*DB filename = //p' $XAPP_LOG | tail -1 | tr -d '[:space:]'")
    if [ -n "$XAPP_DB" ]; then
        scp -q agg_prb.py "$CORE_HOST:/tmp/agg_prb.py"
        if ssh "$CORE_HOST" "python3 /tmp/agg_prb.py '$XAPP_DB' /tmp/$RUN_ID-prb.csv" && \
           scp -q "$CORE_HOST:/tmp/$RUN_ID-prb.csv" "$LOGS/prb_by_second.csv"; then
            ssh "$CORE_HOST" "rm -f '$XAPP_DB' '$XAPP_DB'-wal '$XAPP_DB'-shm /tmp/$RUN_ID-prb.csv"
            echo "  pulled $(wc -l < "$LOGS/prb_by_second.csv" | tr -d ' ') rows; db removed"
        else
            echo "  ERROR: PRB aggregation or transfer failed; preserving $XAPP_DB on the core"
            XAPP_FAILED=1
        fi
    else
        echo "  ERROR: no DB path in the xApp log"
        XAPP_FAILED=1
    fi
fi

wait

if [ "$CHANNEL_ACTIVE" -eq 1 ]; then
    CHANNEL_RC=$(cat "$LOGS/.channel_rc" 2>/dev/null || echo 1)
    rm -f "$LOGS/.channel_rc"
    if [ "$CHANNEL_RC" -ne 0 ] || \
       ! python3 - "$LOGS/channel_state.json" <<'PYEOF'
import json, sys
try:
    state = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    raise SystemExit(1)
raise SystemExit(0 if state.get("success") else 1)
PYEOF
    then
        echo "ERROR: channel schedule did not complete with verified transitions" >&2
        CHANNEL_FAILED=1
    else
        CHANNEL_TRANSITIONS=$(python3 - "$LOGS/channel_state.json" <<'PYEOF'
import json, sys
print(len(json.load(open(sys.argv[1])).get("transitions", [])))
PYEOF
)
        echo "  channel transitions verified: $CHANNEL_TRANSITIONS"
    fi
fi


echo "== 8c/9 write run_timing.json =="
UE_NODE_PAIRS=""
for n in $UE_LIST; do
    UE_NODE_PAIRS="$UE_NODE_PAIRS ue$n=cell$(cell_of "$n")"
done

RUN_ID="$RUN_ID" DURATION="$DURATION" \
SENDERS_START="${SENDERS_START:-}" \
XAPP_START="${XAPP_START:-}" XAPP_WINDOW="${XAPP_WINDOW:-60}" XAPP_DELAY="${XAPP_DELAY:-90}" \
UE_NODE_PAIRS="$UE_NODE_PAIRS" \
python3 - "$ANCHORS" "$LOGS/run_timing.json" <<'PYEOF'
import json, os, sys

anchors_path, out_path = sys.argv[1], sys.argv[2]

def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

nodes = {}
try:
    lines = open(anchors_path).read().splitlines()
except OSError:
    lines = []
for line in lines:
    parts = line.split("\t")
    if len(parts) != 5:
        continue
    name, host, before, raw, after = parts
    epoch_txt, _, wall = raw.partition("|")
    epoch = f(epoch_txt)
    try:
        hh, mm, ss = wall.split(":")
        sod = int(hh) * 3600 + int(mm) * 60 + float(ss)
    except ValueError:
        sod = None
    entry = {"host": host, "epoch_s": epoch, "sod_s": sod,
             "local_before": f(before), "local_after": f(after)}
    if epoch is not None and sod is not None:
        entry["midnight_epoch"] = round(epoch - sod)
    nodes[name] = entry

ue_node = {}
for pair in (os.environ.get("UE_NODE_PAIRS") or "").split():
    ue, _, node = pair.partition("=")
    if ue and node:
        ue_node[ue] = node

doc = {"run_id": os.environ.get("RUN_ID"),
       "duration_s": f(os.environ.get("DURATION")),
       "senders_start_epoch": f(os.environ.get("SENDERS_START")),
       "nodes": nodes, "ue_node": ue_node}
xs = f(os.environ.get("XAPP_START"))
if xs is not None:
    doc["xapp"] = {"start_epoch": xs,
                   "window_s": f(os.environ.get("XAPP_WINDOW")),
                   "delay_s": f(os.environ.get("XAPP_DELAY"))}

with open(out_path, "w") as fh:
    json.dump(doc, fh, indent=2)

mids = {n: v.get("midnight_epoch") for n, v in nodes.items()}
print("  nodes anchored: %s" % list(nodes))
if len(set(v for v in mids.values() if v is not None)) > 1:
    print("  note: node civil-time zones differ; per-node UTC anchors compensate %s" % mids)
PYEOF
rm -f "$ANCHORS"

echo "== 9/9 collect logs =="
ssh "$CORE_HOST" "sudo docker exec $DN_CONTAINER tar czf - -C /logs/mgen dn_dl_tx.log dn_ul_rx.log" \
    > "$LOGS/_core.tgz" 2>/dev/null || echo "  core logs missing"
for n in $UE_LIST; do
    c=$(cell_of "$n"); u=$(ue_of "$n"); h=$(host_of "$c")
    ssh "$h" "sudo docker exec ric5g-ue-cell$c-$u tar czf - -C /logs/mgen ue${n}_dl_rx.log ue${n}_ul_tx.log" \
        > "$LOGS/_ue$n.tgz" 2>/dev/null || echo "  ue$n logs missing" &
done
wait
for t in "$LOGS"/_*.tgz; do [ -s "$t" ] && tar xzf "$t" -C "$LOGS"; rm -f "$t"; done
echo "done: $(ls "$LOGS"/*.log 2>/dev/null | wc -l | tr -d ' ') logs in $LOGS"
for required in dn_dl_tx.log dn_ul_rx.log; do
    [ -s "$LOGS/$required" ] || { echo "ERROR: missing or empty $required" >&2; MGEN_FAILED=1; }
done
for n in $UE_LIST; do
    for suffix in dl_rx ul_tx; do
        required="ue${n}_${suffix}.log"
        [ -s "$LOGS/$required" ] || { echo "ERROR: missing or empty $required" >&2; MGEN_FAILED=1; }
    done
done
if [ "$XAPP_FAILED" -ne 0 ]; then
    echo "ERROR: traffic logs were collected, but the xApp measurement did not close cleanly" >&2
    exit 1
fi
if [ "$MGEN_FAILED" -ne 0 ]; then
    echo "ERROR: the distributed MGEN run did not produce its complete log contract" >&2
    exit 1
fi
if [ "$CHANNEL_FAILED" -ne 0 ]; then
    echo "ERROR: traffic logs were collected, but channel labels are incomplete" >&2
    exit 1
fi
