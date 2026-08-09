from types import SimpleNamespace

import pytest

import clock_guard


NTPQ = """
     remote           refid      st t when poll reach   delay   offset  jitter
==============================================================================
*155.98.33.74    44.190.5.123     3 u   40   64  377    0.174   +0.335   0.138
"""


def test_parse_ntpq_selected_peer():
    peer = clock_guard.parse_ntpq(NTPQ, "cell.example")

    assert peer.host == "cell.example"
    assert peer.server == "155.98.33.74"
    assert peer.reach == int("377", 8)
    assert peer.offset_ms == pytest.approx(0.335)
    assert peer.jitter_ms == pytest.approx(0.138)


def test_parse_ntpq_rejects_missing_selected_peer():
    with pytest.raises(ValueError, match="no selected NTP peer"):
        clock_guard.parse_ntpq(NTPQ.replace("*", "+", 1), "cell.example")


def test_gate_accepts_aligned_nodes():
    peers = {
        "core": clock_guard.Peer("core", "ntp1", 255, -0.05, 0.03),
        "cell1": clock_guard.Peer("cell1", "ntp1", 3, 0.60, 0.84),
    }

    result = clock_guard.gate(
        peers,
        max_abs_offset_ms=5.0,
        max_offset_spread_ms=5.0,
        max_jitter_ms=1.0,
    )

    assert result["passed"] is True
    assert result["offset_spread_ms"] == pytest.approx(0.65)


def test_gate_can_record_jitter_as_warning_after_a_run():
    peers = {
        "core": clock_guard.Peer("core", "ntp1", 255, -0.079, 0.021),
        "cell1": clock_guard.Peer("cell1", "ntp1", 15, 0.199, 3.353),
    }

    result = clock_guard.gate(
        peers,
        max_abs_offset_ms=5.0,
        max_offset_spread_ms=5.0,
        max_jitter_ms=1.0,
        enforce_jitter=False,
    )

    assert result["passed"] is True
    assert result["offset_ok"] is True
    assert result["jitter_ok"] is False
    assert result["jitter_policy"] == "warning"


@pytest.mark.parametrize(
    ("cell", "field"),
    [
        (clock_guard.Peer("cell1", "ntp1", 3, 6.0, 0.2), "max_abs_offset_ms"),
        (clock_guard.Peer("cell1", "ntp1", 3, 0.2, 1.1), "max_jitter_ms"),
        (clock_guard.Peer("cell1", "ntp1", 0, 0.2, 0.1), "reach_ok"),
    ],
)
def test_gate_rejects_bad_node(cell, field):
    peers = {
        "core": clock_guard.Peer("core", "ntp1", 255, -0.05, 0.03),
        "cell1": cell,
    }

    result = clock_guard.gate(
        peers,
        max_abs_offset_ms=5.0,
        max_offset_spread_ms=5.0,
        max_jitter_ms=1.0,
    )

    assert result["passed"] is False
    assert field in result


def test_parse_nodes_requires_unique_named_hosts():
    assert clock_guard.parse_nodes(["core=a", "cell1=b"]) == {
        "core": "a",
        "cell1": "b",
    }
    with pytest.raises(ValueError, match="duplicate node name"):
        clock_guard.parse_nodes(["core=a", "core=b"])


def test_prepare_synchronizes_only_bad_node(monkeypatch):
    before = {
        "core": clock_guard.Peer("core-host", "ntp1", 255, -0.2, 0.1),
        "cell1": clock_guard.Peer("cell-host", "ntp1", 255, 11.8, 1.8),
    }
    after = {
        "core": before["core"],
        "cell1": clock_guard.Peer("cell-host", "ntp1", 1, 0.4, 0.2),
    }
    samples = iter(((before, {}), (after, {})))
    monkeypatch.setattr(clock_guard, "collect", lambda nodes: next(samples))
    synchronized = []
    monkeypatch.setattr(
        clock_guard,
        "synchronize",
        lambda host, server: synchronized.append((host, server)) or "stepped",
    )
    args = SimpleNamespace(
        operation="prepare",
        node=["core=core-host", "cell1=cell-host"],
        server="ntp1",
        max_abs_offset_ms=5.0,
        max_offset_spread_ms=5.0,
        max_jitter_ms=1.0,
        timeout_s=10.0,
        poll_interval_s=0.0,
    )

    result = clock_guard.run(args)

    assert result["passed"] is True
    assert synchronized == [("cell-host", "ntp1")]
    assert result["nodes"]["core"]["action"] == "none"
    assert result["nodes"]["cell1"]["action"] == "synchronized"
