import csv
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import agg_prb


def make_db(path, dual_clock=True):
    db = sqlite3.connect(path)
    receipt = "recv_tstamp INTEGER," if dual_clock else ""
    db.execute(f"""
        CREATE TABLE MAC_UE(
            tstamp INTEGER,
            {receipt}
            nb_id INTEGER,
            rnti INTEGER,
            dl_aggr_prb INTEGER,
            ul_aggr_prb INTEGER,
            dl_mcs1 INTEGER
        )
    """)
    columns = ("tstamp, recv_tstamp, nb_id, rnti, dl_aggr_prb, "
               "ul_aggr_prb, dl_mcs1")
    if dual_clock:
        db.executemany(
            f"INSERT INTO MAC_UE({columns}) VALUES(?, ?, ?, ?, ?, ?, ?)",
            [
                (1_000_000, 10_100_000, 3584, 7, 10, 20, 4),
                (1_500_000, 10_900_000, 3584, 7, 12, 25, 6),
                (2_000_000, 11_200_000, 3584, 7, 15, 30, 8),
            ],
        )
    db.commit()
    db.close()


def test_uses_receipt_clock_and_preserves_source_clock(tmp_path):
    source = tmp_path / "xapp.sqlite"
    output = tmp_path / "prb.csv"
    make_db(source)

    agg_prb.main(source, output)

    with output.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [int(row["utc_second"]) for row in rows] == [10, 11]
    assert int(rows[0]["recv_tstamp_us"]) == 10_900_000
    assert int(rows[0]["source_tstamp_us"]) == 1_500_000
    assert int(rows[0]["samples"]) == 2
    assert float(rows[0]["dl_mcs1_avg"]) == 5.0


def test_rejects_legacy_source_clock_only_database(tmp_path):
    source = tmp_path / "legacy.sqlite"
    make_db(source, dual_clock=False)
    with pytest.raises(SystemExit, match="recv_tstamp"):
        agg_prb.main(source, tmp_path / "prb.csv")
