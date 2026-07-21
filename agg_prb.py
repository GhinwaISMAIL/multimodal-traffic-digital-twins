#!/usr/bin/env python3
"""Aggregate MAC_UE to per-second counters on the core, before transfer.
    python3 agg_prb.py <xapp_db> <out_csv>
Emits one row per (utc_second, nb_id, rnti) holding the last cumulative counter
value in that second, plus averages of any radio columns present.
"""
import csv
import sqlite3
import sys

REQUIRED = ["tstamp", "nb_id", "rnti", "dl_aggr_prb", "ul_aggr_prb"]
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
        SELECT CAST(tstamp / 1000000 AS INTEGER) AS utc_second,
               nb_id,
               rnti,
               MAX(dl_aggr_prb) AS dl_aggr_prb,
               MAX(ul_aggr_prb) AS ul_aggr_prb,
               COUNT(*) AS samples{extra_sel}
        FROM MAC_UE
        GROUP BY utc_second, nb_id, rnti
        ORDER BY utc_second, nb_id, rnti
    """)

    header = ["utc_second", "nb_id", "rnti", "dl_aggr_prb", "ul_aggr_prb",
              "samples"] + [f"{c}_avg" for c in extras]
    n = 0
    with open(out_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for row in rows:
            w.writerow(row)
            n += 1
    db.close()
    print(f"wrote {n} per-second rows to {out_path} (extras: {extras or 'none'})")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: agg_prb.py <xapp_db> <out_csv>")
    main(sys.argv[1], sys.argv[2])
