import importlib.util
import json
import os
import socketserver
import sys
import threading
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
HELPER = Path(os.environ.get("OAI_5G_RIC_REPO", ROOT.parent / "oai-5g-ric")) / "bin" / "channel-cell.py"
pytestmark = pytest.mark.skipif(not HELPER.is_file(), reason="set OAI_5G_RIC_REPO to the oai-5g-ric checkout")

SPEC = importlib.util.spec_from_file_location("channel_schedule", ROOT / "channel_schedule.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

PROMPT = "softmodem_5Gue> "
FAKE_SSH = '''import os, shlex, subprocess, sys
parts = shlex.split(sys.argv[2])
assert parts[:2] == ["sudo", "python3"]
command = [sys.executable] + parts[2:]
for item in os.environ["FAKE_ADDRESSES"].split(","):
    command += ["--address", item]
sys.exit(subprocess.call(command))
'''


class FakeUe(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, ignore_modify=False):
        super().__init__(("127.0.0.1", 0), Handler)
        self.values = {"ploss": 0.0, "noise_power_dB": -30.0}
        self.ignore_modify = ignore_modify
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server_address[1]


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        server = self.server
        self.wfile.write(PROMPT.encode())
        for raw in self.rfile:
            line = raw.decode().strip()
            if line == "channelmod show current":
                text = (f"model 0 rfsimu_channel_enB0 type AWGN:\n"
                        f"path loss: {server.values['ploss']:.6f}  noise: "
                        f"{server.values['noise_power_dB']:.6f} rchannel offset: 0    "
                        f"forget factor; 0.000000\n----------------\n")
                self.wfile.write((text + PROMPT).encode())
            elif line.startswith("channelmod modify 0 "):
                _, _, _, name, value = line.split()
                if not server.ignore_modify:
                    server.values[name] = float(value)
                self.wfile.write(("ok\n" + PROMPT).encode())
            else:
                self.wfile.write(PROMPT.encode())


def setup(tmp_path, monkeypatch, servers):
    script = tmp_path / "fake_ssh.py"
    script.write_text(FAKE_SSH)
    monkeypatch.setattr(MODULE, "SSH", [sys.executable, str(script)])
    monkeypatch.setenv("FAKE_ADDRESSES", ",".join(
        f"{index}=127.0.0.1:{server.port}" for index, server in enumerate(servers, start=1)))
    ue_map = {f"ue{index}": {"cell": 1, "ue": index, "pdu_ip": "12.1.1.2"}
              for index in range(1, len(servers) + 1)}
    return ue_map, {1: "cell-host"}


def schedule(*rows):
    return {"schema_version": 1, "enabled": True, "expected_model_type": "AWGN",
            "events": [{"at_s": at, "target": target, "direction": "dl",
                        "parameter": parameter, "value": value}
                       for at, target, parameter, value in rows]}


def test_batch_run_applies_every_change_near_its_deadline(tmp_path, monkeypatch):
    servers = [FakeUe() for _ in range(3)]
    ue_map, hosts = setup(tmp_path, monkeypatch, servers)
    plan = schedule(*[(at, f"ue{ue}", parameter, float(at + ue))
                      for at in (0.0, 0.5, 1.0) for ue in (1, 2, 3)
                      for parameter in ("ploss", "noise_power_dB")])
    events = MODULE.normalize(plan, ue_map, hosts)
    output = tmp_path / "channel_state.json"
    state = MODULE.execute(plan, events, hosts, str(HELPER.parent), time.time() + 1.5, output)
    saved = json.loads(output.read_text())
    assert state["success"] and saved["success"]
    assert saved["mode"] == "batch"
    assert len(saved["transitions"]) == 18
    assert all(row["status"] == "verified" and row["verified"] for row in saved["transitions"])
    assert saved["max_lag_s"] < 0.5
    assert [server.values["ploss"] for server in servers] == [1.0 + 1, 1.0 + 2, 1.0 + 3]
    assert {item["model_type"] for item in saved["initial_state"]} == {"AWGN"}
    first = saved["transitions"][0]
    assert {"scheduler_apply_epoch", "applied_epoch", "observed", "requested", "target"} <= set(first)


def test_unverified_change_fails_the_run_and_is_recorded(tmp_path, monkeypatch):
    servers = [FakeUe(ignore_modify=True)]
    ue_map, hosts = setup(tmp_path, monkeypatch, servers)
    plan = schedule((0.0, "ue1", "ploss", 4.0), (0.2, "ue1", "ploss", 5.0))
    events = MODULE.normalize(plan, ue_map, hosts)
    output = tmp_path / "channel_state.json"
    with pytest.raises(RuntimeError, match="channel change failed"):
        MODULE.execute(plan, events, hosts, str(HELPER.parent), time.time() + 1.0, output)
    saved = json.loads(output.read_text())
    assert saved["success"] is False
    assert saved["transitions"][0]["status"] == "failed"


def test_batch_mode_rejects_uplink_events(tmp_path, monkeypatch):
    servers = [FakeUe()]
    ue_map, hosts = setup(tmp_path, monkeypatch, servers)
    plan = {"schema_version": 1, "enabled": True, "expected_model_type": "AWGN",
            "events": [{"at_s": 0, "target": "cell1", "direction": "ul",
                        "parameter": "ploss", "value": 3.0}]}
    events = MODULE.normalize(plan, ue_map, hosts)
    with pytest.raises(ValueError, match="downlink changes only"):
        MODULE.check_batch(events, hosts, str(HELPER.parent))
