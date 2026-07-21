#!/usr/bin/env python3
"""Splice the run_timing.json capture into deploy_ric5g.sh.

    python3 apply_timing_patch.py [path/to/deploy_ric5g.sh]

Inserts four blocks at anchors found in the file, backs the original up, and
refuses to run twice. Verifies the result with `bash -n` if bash is present.
"""
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

TARGET = Path(sys.argv[1] if len(sys.argv) > 1 else "deploy_ric5g.sh")

ANCHORS_BLOCK = '''
echo "== 2b/9 capture node clock anchors =="
ANCHORS="$LOGS/.anchors"
: > "$ANCHORS"

anchor_node() {   # name host
    local name=$1 host=$2 before after raw
    before=$(date +%s.%N)
    raw=$(ssh "$host" "date +'%s.%N|%H:%M:%S.%N'")
    after=$(date +%s.%N)
    printf '%s\\t%s\\t%s\\t%s\\t%s\\n' "$name" "$host" "$before" "$raw" "$after" >> "$ANCHORS"
    echo "  $name $raw"
}

anchor_node core "$CORE_HOST"
for _c in $(seq 1 "$NUM_CELLS"); do
    anchor_node "cell$_c" "$(host_of "$_c")"
done
'''

SENDERS_LINE = '''SENDERS_START=$(ssh "$CORE_HOST" "date +%s.%N")   # core clock, not the workstation's
'''

XAPP_LINE = '''    XAPP_START=$(ssh "$CORE_HOST" "date +%s.%N")
'''

WRITE_BLOCK = '''
echo "== 8c/9 write run_timing.json =="
UE_NODE_PAIRS=""
for n in $UE_LIST; do
    UE_NODE_PAIRS="$UE_NODE_PAIRS ue$n=cell$(cell_of "$n")"
done

RUN_ID="$RUN_ID" DURATION="$DURATION" \\
SENDERS_START="${SENDERS_START:-}" \\
XAPP_START="${XAPP_START:-}" XAPP_WINDOW="${XAPP_WINDOW:-60}" XAPP_DELAY="${XAPP_DELAY:-90}" \\
UE_NODE_PAIRS="$UE_NODE_PAIRS" \\
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
    parts = line.split("\\t")
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
    print("  WARNING: node local midnights disagree %s - check time zones" % mids)
PYEOF
rm -f "$ANCHORS"
'''


def die(msg):
    print("ERROR: %s" % msg, file=sys.stderr)
    sys.exit(1)


def main():
    if not TARGET.exists():
        die("%s not found - run this from the repo holding deploy_ric5g.sh" % TARGET)

    src = TARGET.read_text()
    if "run_timing.json" in src or "anchor_node" in src:
        die("already patched (found anchor_node / run_timing.json) - nothing to do")

    out = src

    # 1) clock anchors, right after the phase-2 UE table is printed
    m = re.search(r'^.*column -t "\$LOGS/ue_ips\.txt".*$', out, re.M)
    if not m:
        die("cannot find the phase-2 anchor line (column -t .../ue_ips.txt)")
    out = out[:m.end()] + "\n" + ANCHORS_BLOCK + out[m.end():]

    # 2) senders start, immediately before the first phase-8 sender ssh
    m = re.search(r'^(\s*)(ssh "\$CORE_HOST".*dn_dl_tx\.mgn.*)$', out, re.M)
    if not m:
        die("cannot find the phase-8 sender line (dn_dl_tx.mgn)")
    out = out[:m.start()] + SENDERS_LINE + out[m.start():]

    # 3) xApp start, immediately before the xApp is launched
    m = re.search(r'^\s*ssh -n "\$CORE_HOST".*\$XAPP_BIN.*$', out, re.M)
    if not m:
        die("cannot find the xApp launch line ($XAPP_BIN)")
    out = out[:m.start()] + XAPP_LINE + out[m.start():]

    # 4) write the JSON just before the collect phase
    m = re.search(r'^echo "== 9/9 collect logs ==".*$', out, re.M)
    if not m:
        die('cannot find the phase-9 line (echo "== 9/9 collect logs ==")')
    out = out[:m.start()] + WRITE_BLOCK + "\n" + out[m.start():]

    backup = TARGET.with_suffix(".sh.bak.%d" % int(time.time()))
    shutil.copy2(TARGET, backup)
    TARGET.write_text(out)

    ok = True
    if shutil.which("bash"):
        r = subprocess.run(["bash", "-n", str(TARGET)],
                           capture_output=True, text=True)
        ok = r.returncode == 0
        if not ok:
            print(r.stderr.strip(), file=sys.stderr)

    print("patched   : %s" % TARGET)
    print("backup    : %s" % backup)
    print("inserts   : anchors, SENDERS_START, XAPP_START, run_timing.json")
    print("bash -n   : %s" % ("OK" if ok else "FAILED - restore the backup"))
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
