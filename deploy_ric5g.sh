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
CELL_HOSTS=("${CELL1_HOST:-ghinwa@pc05-fort.emulab.net}" "${CELL2_HOST:-ghinwa@pc11-fort.emulab.net}")
UES_PER_CELL="${UES_PER_CELL:-12}"
NUM_CELLS="${NUM_CELLS:-2}"
HOST_LOG_DIR=/local/logs/mgen
REMOTE_BIN=/local/repository/bin
RUN_ID="mgen-$(date +%Y%m%d-%H%M%S)"

SCRIPTS="$RUN_DIR/mgen_scripts"
LOGS="$RUN_DIR/logs"
mkdir -p "$LOGS"
[ -d "$SCRIPTS" ] || { echo "no mgen_scripts in $RUN_DIR" >&2; exit 1; }

cell_of()  { echo $(( ($1 - 1) / UES_PER_CELL + 1 )); }
ue_of()    { echo $(( ($1 - 1) % UES_PER_CELL + 1 )); }
host_of()  { echo "${CELL_HOSTS[$(( $1 - 1 ))]}"; }
nb_of()    { echo $(( 3583 + $1 )); }

UE_LIST=$(ls "$SCRIPTS" | sed -n 's/^ue\([0-9]\{1,\}\)_ul_tx\.mgn$/\1/p' | sort -n)
[ -n "$UE_LIST" ] || { echo "no ue*_ul_tx.mgn found" >&2; exit 1; }
echo "run_id=$RUN_ID  duration=${DURATION}s  ues=$(echo "$UE_LIST" | wc -l | tr -d ' ')"

echo "== 1/9 check core =="
ssh "$CORE_HOST" "sudo bash $REMOTE_BIN/mgen-core.sh check"

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

echo "== 3/9 rewrite DN downlink with live IPs =="
cp "$SCRIPTS/dn_dl_tx.mgn" "$LOGS/dn_dl_tx.mgn"
if [ -f "$SCRIPTS/manifest.csv" ]; then
    while read -r name c u live; do
        designed=$(awk -F, -v n="$name" 'NR>1 && $0 ~ n {for(i=1;i<=NF;i++) if($i ~ /^12\.1\.1\./) {print $i; exit}}' \
                   "$SCRIPTS/manifest.csv" || true)
        if [ -n "$designed" ] && [ "$designed" != "$live" ]; then
            sed -i '' "s#DST $designed/#DST $live/#g" "$LOGS/dn_dl_tx.mgn"
            echo "  $name: $designed -> $live"
        fi
    done < "$LOGS/ue_ips.txt"
fi

echo "== 4/9 snapshot UE -> RNTI =="
: > "$LOGS/rnti_map.csv"
echo "ue,cell,ue_index,nb_id,rnti,pdu_ip" >> "$LOGS/rnti_map.csv"
for n in $UE_LIST; do
    c=$(cell_of "$n"); u=$(ue_of "$n"); h=$(host_of "$c")
    ip=$(awk -v k="ue$n" '$1==k{print $4}' "$LOGS/ue_ips.txt")
    hex=$(ssh "$h" "sudo docker logs ric5g-ue-cell$c-$u 2>&1 | grep -oE 'RNTI [0-9a-f]{4}' | tail -1 | cut -d' ' -f2" || true)
    [ -n "$hex" ] || { echo "  WARN: ue$n has no RNTI in its log"; continue; }
    echo "ue$n,$c,$u,$(nb_of "$c"),$((16#$hex)),$ip" >> "$LOGS/rnti_map.csv"
done
sed 's/^/  /' "$LOGS/rnti_map.csv"

echo "== 5/9 push scripts =="
push() {  # host container file
    local h=$1 ctr=$2 f=$3 b
    b=$(basename "$f")
    scp -q "$f" "$h:/tmp/$b"
    ssh "$h" "sudo docker exec $ctr mkdir -p /logs/mgen && sudo docker cp /tmp/$b $ctr:/logs/mgen/$b"
}
push "$CORE_HOST" ric5g-oai-ext-dn "$LOGS/dn_dl_tx.mgn"
push "$CORE_HOST" ric5g-oai-ext-dn "$SCRIPTS/dn_ul_rx.mgn"
for n in $UE_LIST; do
    c=$(cell_of "$n"); u=$(ue_of "$n"); h=$(host_of "$c")
    push "$h" "ric5g-ue-cell$c-$u" "$SCRIPTS/ue${n}_ul_tx.mgn"
    push "$h" "ric5g-ue-cell$c-$u" "$SCRIPTS/ue${n}_dl_rx.mgn"
done

echo "== 6/9 teardown stale mgen =="
ssh "$CORE_HOST" "sudo docker exec ric5g-oai-ext-dn pkill -9 mgen || true" >/dev/null 2>&1
for n in $UE_LIST; do
    c=$(cell_of "$n"); u=$(ue_of "$n"); h=$(host_of "$c")
    ssh "$h" "sudo docker exec ric5g-ue-cell$c-$u pkill -9 mgen || true" >/dev/null 2>&1 &
done
wait

echo "== 7/9 arm receivers =="
ssh "$CORE_HOST" "sudo bash $REMOTE_BIN/mgen-core.sh run-script $RUN_ID dn_ul_rx.mgn $((DURATION + 30)) rx"
for n in $UE_LIST; do
    c=$(cell_of "$n"); u=$(ue_of "$n"); h=$(host_of "$c")
    ssh "$h" "sudo bash $REMOTE_BIN/mgen-cell.sh run-script $RUN_ID $c $u ue${n}_dl_rx.mgn $((DURATION + 30)) rx" &
done
wait
sleep 5

if [ "${XAPP:-0}" = 1 ]; then
    echo "== 7b/9 verify RIC is ready =="
    ASSOC=$(ssh "$CORE_HOST" "sudo awk 'NR>1 && \$12==36421 {c++} END {print c+0}' /proc/net/sctp/assocs")
    STALE=$(ssh "$CORE_HOST" "sudo awk 'NR>1 && (\$12==36422 || \$13==36422) {c++} END {print c+0}' /proc/net/sctp/assocs")
    echo "  e2_associations=$ASSOC  stale_e42=$STALE"
    [ "$STALE" -eq 0 ] || { echo "stale E42 associations on the RIC - restart it before running" >&2; exit 1; }
fi

echo "== 8/9 start senders =="
ssh "$CORE_HOST" "sudo bash $REMOTE_BIN/mgen-core.sh run-script $RUN_ID dn_dl_tx.mgn $DURATION tx" &
for n in $UE_LIST; do
    c=$(cell_of "$n"); u=$(ue_of "$n"); h=$(host_of "$c")
    ssh "$h" "sudo bash $REMOTE_BIN/mgen-cell.sh run-script $RUN_ID $c $u ue${n}_ul_tx.mgn $DURATION tx" &
done
echo "traffic running for ${DURATION}s"

if [ "${XAPP:-0}" = 1 ]; then
    XAPP_DIR=/opt/oai-src/openair2/E2AP/flexric
    XAPP_BIN=./build/examples/xApp/c/monitor/xapp_gtp_mac_rlc_pdcp_moni
    XAPP_LOG="/local/logs/xapp-$RUN_ID.log"
    WANT_SUBS="${XAPP_SUBS:-8}"
    DELAY="${XAPP_DELAY:-90}"
    WINDOW="${XAPP_WINDOW:-60}"
    echo "== 8b/9 xApp window: +${DELAY}s for ${WINDOW}s =="
    sleep "$DELAY"
    ssh -n "$CORE_HOST" "cd $XAPP_DIR && nohup stdbuf -oL -eL $XAPP_BIN > $XAPP_LOG 2>&1 & echo started" >/dev/null
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
        echo "  WARN: only $SUBS/$WANT_SUBS subscriptions, stopping xApp"
    fi
    ssh "$CORE_HOST" "pkill -INT -f '[x]app_gtp_mac_rlc_pdcp_moni' || true"
    for _ in $(seq 1 20); do
        ssh "$CORE_HOST" "pgrep -f '[x]app_gtp_mac_rlc_pdcp_moni' >/dev/null" || break
        sleep 2
    done
    DELS=$(ssh "$CORE_HOST" "grep -c 'SUBSCRIPTION DELETE RESPONSE rx' $XAPP_LOG 2>/dev/null || true" | head -1)
    [ -n "$DELS" ] || DELS=0
    echo "  delete responses: $DELS/$WANT_SUBS"
    XAPP_DB=$(ssh "$CORE_HOST" "sed -n 's/.*DB filename = //p' $XAPP_LOG | tail -1 | tr -d '[:space:]'")
    if [ -n "$XAPP_DB" ]; then
        scp -q agg_prb.py "$CORE_HOST:/tmp/agg_prb.py"
        ssh "$CORE_HOST" "python3 /tmp/agg_prb.py '$XAPP_DB' /tmp/$RUN_ID-prb.csv" || true
        scp -q "$CORE_HOST:/tmp/$RUN_ID-prb.csv" "$LOGS/prb_by_second.csv" || echo "  WARN: no PRB csv"
        scp -q "$CORE_HOST:$XAPP_LOG" "$LOGS/xapp.log" || true
        ssh "$CORE_HOST" "rm -f '$XAPP_DB' '$XAPP_DB'-wal '$XAPP_DB'-shm /tmp/$RUN_ID-prb.csv"
        [ -f "$LOGS/prb_by_second.csv" ] && \
            echo "  pulled $(wc -l < "$LOGS/prb_by_second.csv" | tr -d ' ') rows; db removed"
    else
        echo "  WARN: no DB path in the xApp log"
    fi
fi

wait

echo "== 9/9 collect logs =="
ssh "$CORE_HOST" "sudo docker exec ric5g-oai-ext-dn tar czf - -C /logs/mgen dn_dl_tx.log dn_ul_rx.log" \
    > "$LOGS/_core.tgz" 2>/dev/null || echo "  core logs missing"
for n in $UE_LIST; do
    c=$(cell_of "$n"); u=$(ue_of "$n"); h=$(host_of "$c")
    ssh "$h" "sudo docker exec ric5g-ue-cell$c-$u tar czf - -C /logs/mgen ue${n}_dl_rx.log ue${n}_ul_tx.log" \
        > "$LOGS/_ue$n.tgz" 2>/dev/null || echo "  ue$n logs missing" &
done
wait
for t in "$LOGS"/_*.tgz; do [ -s "$t" ] && tar xzf "$t" -C "$LOGS"; rm -f "$t"; done
echo "done: $(ls "$LOGS"/*.log 2>/dev/null | wc -l | tr -d ' ') logs in $LOGS"
