import csv
import json
from pathlib import Path

import pytest

from parse_ue_radio import parse_line, parse_logs, write_csv


def test_parse_line_reads_versioned_record():
    row = parse_line(
        "12.0 [PHY] I UE_RADIO_V1 utc_second=100 emitted_epoch_us=101030000 "
        "ue=0 cell=0 ssb=0 samples=15 ss_rsrp_dbm=-51.000 "
        "ss_rsrq_db=-10.460 ss_sinr_db=38.100"
    )

    assert row["utc_second"] == 100
    assert row["ss_rsrp_dbm"] == -51
    assert row["samples"] == 15


def test_parse_logs_maps_external_ue_and_filters_run_window(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "rnti_map.csv").write_text(
        "ue,cell,ue_index,nb_id,rnti,pdu_ip\nue1,1,1,3584,10,12.1.1.2\n"
    )
    (logs / "ue1_radio.log").write_text("\n".join(
        f"UE_RADIO_V1 utc_second={second} emitted_epoch_us={(second + 1) * 1000000 + 30000} "
        "ue=0 cell=0 ssb=0 samples=15 ss_rsrp_dbm=-41 "
        "ss_rsrq_db=-10.47 ss_sinr_db=48.1"
        for second in range(99, 112)
    ))
    timing = logs / "run_timing.json"
    timing.write_text(json.dumps({
        "senders_start_epoch": 100.2, "duration_s": 10,
    }))

    rows = parse_logs(logs, timing)

    assert {row["utc_second"] for row in rows} == set(range(100, 111))
    assert {row["ue"] for row in rows} == {"ue1"}
    assert {row["cell"] for row in rows} == {1}
    output = logs / "ue_radio_by_second.csv"
    write_csv(output, rows)
    with output.open(newline="") as stream:
        written = list(csv.DictReader(stream))
    assert len(written) == 11


def test_parse_logs_rejects_insufficient_coverage(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "rnti_map.csv").write_text(
        "ue,cell,ue_index,nb_id,rnti,pdu_ip\nue1,1,1,3584,10,12.1.1.2\n"
    )
    (logs / "ue1_radio.log").write_text(
        "UE_RADIO_V1 utc_second=100 emitted_epoch_us=101030000 ue=0 cell=0 "
        "ssb=0 samples=15 ss_rsrp_dbm=-41 ss_rsrq_db=-10.47 ss_sinr_db=48.1\n"
    )
    timing = logs / "run_timing.json"
    timing.write_text(json.dumps({
        "senders_start_epoch": 100.0, "duration_s": 180,
    }))

    with pytest.raises(ValueError, match="minimum"):
        parse_logs(logs, timing)
