from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import channel_schedule


def mapping(tmp_path):
    path = tmp_path / "ue_ips.txt"
    path.write_text("ue1 1 1 12.1.1.2\nue2 1 2 12.1.1.3\nue3 2 1 12.1.1.4\n")
    return channel_schedule.load_ue_map(path)


def test_normalize_maps_global_ues_and_cell_uplink(tmp_path):
    schedule = {
        "schema_version": 1,
        "events": [
            {"at_s": 10, "target": "ue3", "direction": "dl",
             "parameter": "ploss", "value": 12},
            {"at_s": 5, "target": "cell1", "direction": "ul",
             "parameter": "noise_power_dB", "value": -25},
        ],
    }
    events = channel_schedule.normalize(
        schedule, mapping(tmp_path), {1: "cell1", 2: "cell2"}, duration=30)
    assert [event["target"] for event in events] == ["cell1", "ue3"]
    assert events[0]["ue"] is None
    assert events[1]["cell"] == 2
    assert events[1]["ue"] == 1


@pytest.mark.parametrize("event", [
    {"at_s": -1, "target": "ue1", "direction": "dl",
     "parameter": "ploss", "value": 1},
    {"at_s": 31, "target": "ue1", "direction": "dl",
     "parameter": "ploss", "value": 1},
    {"at_s": 1, "target": "cell1", "direction": "dl",
     "parameter": "ploss", "value": 1},
    {"at_s": 1, "target": "ue1", "direction": "ul",
     "parameter": "ploss", "value": 1},
])
def test_invalid_schedule_is_rejected(tmp_path, event):
    with pytest.raises(ValueError):
        channel_schedule.normalize(
            {"schema_version": 1, "events": [event]},
            mapping(tmp_path), {1: "cell1", 2: "cell2"}, duration=30)


def test_disabled_schedule_has_no_events(tmp_path):
    assert channel_schedule.normalize(
        {"schema_version": 1, "enabled": False}, mapping(tmp_path),
        {1: "cell1", 2: "cell2"}) == []
