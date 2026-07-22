#!/usr/bin/env python3
"""Aggregate MAC_UE to wall-clock seconds on the core, before transfer.
    python3 agg_prb.py <xapp_db> <out_csv>
Emits one row per (core receipt second, nb_id, rnti), preserving both the core
receipt timestamp and the RFsim/service-model timestamp.  MGEN and channel
events must be joined with ``utc_second``; ``source_tstamp_us`` is diagnostic
radio time and must never be treated as UTC.
"""
import csv
import sqlite3
import sys

REQUIRED = ["tstamp", "recv_tstamp", "nb_id", "rnti",
            "dl_aggr_prb", "ul_aggr_prb"]
OPTIONAL = ["dl_mcs1", "ul_mcs1", "pusch_snr", "pucch_snr",
            "dl_bler", "ul_bler", "wb_cqi", "phr", "bsr"]


def main(db_path, out_path):
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cols = {r[1] for r in db.execute('PRAGMA table_info("MAC_UE")')}

    missing = [c for c in REQUIRED if c not in cols]
    if missing:
        sys.exit(f"MAC_UE is missing required columns: {missing}")

    extras = [c for c in OPTIONAL if c in cols]
    extra_sel = "".join(f", AVG({c}) AS {c}_avg" for c in extras)

    rows = db.execute(f"""
        SELECT CAST(recv_tstamp / 1000000 AS INTEGER) AS utc_second,
               MAX(recv_tstamp) AS recv_tstamp_us,
               MAX(tstamp) AS source_tstamp_us,
               nb_id,
               rnti,
               MAX(dl_aggr_prb) AS dl_aggr_prb,
               MAX(ul_aggr_prb) AS ul_aggr_prb,
               COUNT(*) AS samples{extra_sel}
        FROM MAC_UE
        GROUP BY utc_second, nb_id, rnti
        ORDER BY utc_second, nb_id, rnti
    """)

    header = ["utc_second", "recv_tstamp_us", "source_tstamp_us",
              "nb_id", "rnti", "dl_aggr_prb", "ul_aggr_prb", "samples"]
    header += [f"{c}_avg" for c in extras]
    n = 0
    first_recv = last_recv = first_source = last_source = None
    with open(out_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for row in rows:
            w.writerow(row)
            n += 1
            recv = int(row[1])
            source = int(row[2])
            first_recv = recv if first_recv is None else min(first_recv, recv)
            last_recv = recv if last_recv is None else max(last_recv, recv)
            first_source = source if first_source is None else min(first_source, source)
            last_source = source if last_source is None else max(last_source, source)
    db.close()
    print(f"wrote {n} per-second rows to {out_path} (extras: {extras or 'none'})")
    if n > 0 and last_recv > first_recv:
        wall_span = (last_recv - first_recv) / 1_000_000
        source_span = (last_source - first_source) / 1_000_000
        print(
            f"clock spans: receipt={wall_span:.3f}s "
            f"source={source_span:.3f}s source/wall={source_span / wall_span:.6f}"
        )


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: agg_prb.py <xapp_db> <out_csv>")
    main(sys.argv[1], sys.argv[2])
