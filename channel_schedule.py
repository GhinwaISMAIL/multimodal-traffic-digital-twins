#!/usr/bin/env python3
"""Validate and execute a verified RFsim channel schedule over SSH.

The schedule clock is relative to traffic start. Every change is applied by
the cell-side ``channel-cell.py`` helper, which reads the value back before it
reports success. The resulting JSON is therefore suitable as a dataset label:
it contains both the requested transition and the actual remote UTC epoch.

Two execution modes exist. ``batch`` (default) sends each cell its whole
timeline in one SSH session and the helper applies it over persistent telnet
connections, so changes are applied within milliseconds of their deadline and
each record carries ``lag_s``. ``sequential`` starts one SSH session per
change; it needs about 3.3 s per change and cannot follow a timeline that has
more than one change every few seconds. Batch mode needs the ``play`` and
``check`` operations of ``channel-cell.py`` on the cell nodes; set
``CHANNEL_SCHEDULE_MODE=sequential`` to keep using nodes that lack them.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import threading
import time


PARAMETERS = {
    "noise_power_dB", "ploss", "riceanf", "aoa", "offset", "forgetf"
}
SSH = ["ssh"]
FLUSH_S = 2.0


def load_json(path: str | Path) -> dict:
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def load_ue_map(path: str | Path) -> dict[str, dict]:
    rows = {}
    for line in Path(path).read_text().splitlines():
        fields = line.split()
        if len(fields) != 4:
            raise ValueError(f"invalid UE mapping line: {line!r}")
        name, cell, ue, address = fields
        rows[name] = {
            "cell": int(cell), "ue": int(ue), "pdu_ip": address,
        }
    if not rows:
        raise ValueError("UE mapping is empty")
    return rows


def parse_hosts(values: list[str]) -> dict[int, str]:
    hosts = {}
    for value in values:
        key, separator, host = value.partition("=")
        if not separator or not key.isdigit() or not host:
            raise ValueError(f"invalid --cell-host value: {value!r}")
        hosts[int(key)] = host
    return hosts


def normalize(schedule: dict, ue_map: dict[str, dict], hosts: dict[int, str],
              duration: float | None = None) -> list[dict]:
    if schedule.get("schema_version") != 1:
        raise ValueError("channel schedule schema_version must be 1")
    if not schedule.get("enabled", True):
        return []
    raw_events = schedule.get("events")
    if not isinstance(raw_events, list) or not raw_events:
        raise ValueError("enabled channel schedule must contain events")

    events = []
    identities = set()
    for index, raw in enumerate(raw_events):
        if not isinstance(raw, dict):
            raise ValueError(f"event {index} must be an object")
        at_s = float(raw.get("at_s"))
        if at_s < 0 or (duration is not None and at_s > duration):
            raise ValueError(f"event {index} at_s={at_s} is outside the traffic run")
        direction = raw.get("direction")
        parameter = raw.get("parameter")
        target = str(raw.get("target") or "")
        if direction not in {"dl", "ul"}:
            raise ValueError(f"event {index} has invalid direction: {direction!r}")
        if parameter not in PARAMETERS:
            raise ValueError(f"event {index} has invalid parameter: {parameter!r}")
        value = float(raw.get("value"))

        if direction == "dl":
            if not re.fullmatch(r"ue\d+", target) or target not in ue_map:
                raise ValueError(f"DL event {index} has unknown UE target: {target!r}")
            cell = ue_map[target]["cell"]
            ue = ue_map[target]["ue"]
        else:
            match = re.fullmatch(r"cell(\d+)", target)
            if not match:
                raise ValueError(
                    f"UL event {index} target must be cell<N>, got {target!r}")
            cell = int(match.group(1))
            ue = None
        if cell not in hosts:
            raise ValueError(f"event {index} has no SSH host for cell {cell}")

        identity = (at_s, target, direction, parameter)
        if identity in identities:
            raise ValueError(f"duplicate channel transition: {identity}")
        identities.add(identity)
        events.append({
            "event_index": index, "at_s": at_s, "target": target,
            "cell": cell, "ue": ue, "direction": direction,
            "parameter": parameter, "value": value,
        })
    return sorted(events, key=lambda row: (row["at_s"], row["event_index"]))


def remote(event: dict, hosts: dict[int, str], remote_bin: str,
           operation: str) -> dict:
    command = [
        "sudo", "python3", f"{remote_bin}/channel-cell.py", operation,
        "--cell", str(event["cell"]), "--direction", event["direction"],
        "--parameter", event["parameter"],
    ]
    if event["ue"] is not None:
        command.extend(["--ue", str(event["ue"])])
    if operation == "set":
        command.extend(["--value", str(event["value"])])
    output = subprocess.check_output(
        ["ssh", hosts[event["cell"]], shlex.join(command)],
        text=True, stderr=subprocess.STDOUT)
    for line in reversed(output.splitlines()):
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict):
            return result
    raise RuntimeError(f"remote helper returned no JSON: {output.strip()}")


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def check(events: list[dict], hosts: dict[int, str], remote_bin: str,
          expected_model_type: str | None = None) -> list[dict]:
    results = []
    seen = set()
    for event in events:
        identity = (event["cell"], event["ue"], event["direction"],
                    event["parameter"])
        if identity in seen:
            continue
        seen.add(identity)
        result = remote(event, hosts, remote_bin, "show")
        if expected_model_type and result.get("model_type") != expected_model_type:
            raise RuntimeError(
                f"{event['target']} uses {result.get('model_type')}, expected "
                f"{expected_model_type}")
        result["target"] = event["target"]
        results.append(result)
    return results


def execute_sequential(schedule: dict, events: list[dict], hosts: dict[int, str],
                       remote_bin: str, start_epoch: float, output: Path) -> dict:
    state = {
        "schema_version": 1,
        "schedule": schedule,
        "mode": "sequential",
        "traffic_start_reference_epoch": start_epoch,
        "scheduler_started_epoch": time.time(),
        "initial_state": [],
        "transitions": [],
        "success": False,
    }
    atomic_json(output, state)
    try:
        state["initial_state"] = check(
            events, hosts, remote_bin, schedule.get("expected_model_type"))
        atomic_json(output, state)
        for event in events:
            deadline = start_epoch + event["at_s"]
            delay = deadline - time.time()
            if delay > 0:
                time.sleep(delay)
            transition = dict(event)
            transition["scheduler_apply_epoch"] = time.time()
            try:
                transition.update(remote(event, hosts, remote_bin, "set"))
                transition["status"] = "verified"
            except Exception as exc:
                transition["status"] = "failed"
                transition["error"] = str(exc)
                state["transitions"].append(transition)
                atomic_json(output, state)
                raise
            state["transitions"].append(transition)
            atomic_json(output, state)
        state["success"] = True
        return state
    finally:
        state["completed_epoch"] = time.time()
        atomic_json(output, state)


def execute(schedule: dict, events: list[dict], hosts: dict[int, str],
            remote_bin: str, start_epoch: float, output: Path,
            mode: str = "batch") -> dict:
    run = execute_batch if mode == "batch" else execute_sequential
    return run(schedule, events, hosts, remote_bin, start_epoch, output)


def helper_command(remote_bin: str, operation: str, cell: int) -> str:
    return shlex.join(["sudo", "python3", f"{remote_bin}/channel-cell.py",
                       operation, "--cell", str(cell)])


def require_downlink(events: list[dict]) -> None:
    uplink = [event for event in events if event["direction"] != "dl"]
    if uplink:
        raise ValueError(
            "batch mode applies downlink changes only; the cell helper rejects "
            f"uplink control (event {uplink[0]['event_index']})")


def by_cell(events: list[dict]) -> dict[int, list[dict]]:
    groups: dict[int, list[dict]] = {}
    for event in events:
        groups.setdefault(event["cell"], []).append(event)
    return groups


def check_batch(events: list[dict], hosts: dict[int, str], remote_bin: str,
                expected_model_type: str | None = None) -> list[dict]:
    require_downlink(events)
    results = []
    for cell, rows in by_cell(events).items():
        targets = [{"target": row["target"], "ue": row["ue"],
                    "parameter": row["parameter"]} for row in rows]
        output = subprocess.check_output(
            SSH + [hosts[cell], helper_command(remote_bin, "check", cell)],
            input=json.dumps({"targets": targets}), text=True,
            stderr=subprocess.STDOUT)
        for line in reversed(output.splitlines()):
            try:
                answer = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(answer, dict) and "targets" in answer:
                break
        else:
            raise RuntimeError(f"remote helper returned no JSON: {output.strip()}")
        for result in answer["targets"]:
            if expected_model_type and result.get("model_type") != expected_model_type:
                raise RuntimeError(
                    f"{result['target']} uses {result.get('model_type')}, expected "
                    f"{expected_model_type}")
            results.append(result)
    return results


def collect(process: subprocess.Popen, state: dict, lock: threading.Lock,
            failures: list) -> None:
    for line in process.stdout:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or "event_index" not in record:
            continue
        with lock:
            state["transitions"].append(record)
            if record.get("status") != "verified":
                failures.append(record)
    process.wait()
    if process.returncode and not failures:
        failures.append({"status": "failed", "error": process.stderr.read().strip()
                         or f"helper exited with {process.returncode}"})


def execute_batch(schedule: dict, events: list[dict], hosts: dict[int, str],
                  remote_bin: str, start_epoch: float, output: Path) -> dict:
    state = {
        "schema_version": 1,
        "schedule": schedule,
        "mode": "batch",
        "traffic_start_reference_epoch": start_epoch,
        "scheduler_started_epoch": time.time(),
        "initial_state": [],
        "transitions": [],
        "success": False,
    }
    atomic_json(output, state)
    lock = threading.Lock()
    failures: list[dict] = []
    processes = []
    try:
        state["initial_state"] = check_batch(
            events, hosts, remote_bin, schedule.get("expected_model_type"))
        atomic_json(output, state)
        threads = []
        for cell, rows in by_cell(events).items():
            process = subprocess.Popen(
                SSH + [hosts[cell], helper_command(remote_bin, "play", cell)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            processes.append(process)
            process.stdin.write(json.dumps({"start_epoch": start_epoch, "events": rows}))
            process.stdin.close()
            thread = threading.Thread(
                target=collect, args=(process, state, lock, failures), daemon=True)
            thread.start()
            threads.append(thread)
        while any(thread.is_alive() for thread in threads):
            for thread in threads:
                thread.join(FLUSH_S / len(threads))
            with lock:
                atomic_json(output, state)
            if failures:
                for process in processes:
                    process.terminate()
        with lock:
            state["transitions"].sort(key=lambda row: (row["at_s"], row["event_index"]))
            lags = [row["lag_s"] for row in state["transitions"] if "lag_s" in row]
            state["max_lag_s"] = max(lags) if lags else 0.0
            if failures:
                raise RuntimeError(f"channel change failed: {failures[0]}")
            if len(state["transitions"]) != len(events):
                raise RuntimeError(
                    f"{len(state['transitions'])} of {len(events)} changes were reported")
            state["success"] = True
        return state
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
        with lock:
            state["completed_epoch"] = time.time()
            atomic_json(output, state)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("check", "run"))
    parser.add_argument("--schedule", required=True)
    parser.add_argument("--ue-map", required=True)
    parser.add_argument("--cell-host", action="append", default=[], required=True)
    parser.add_argument("--remote-bin", default="/local/repository/bin")
    parser.add_argument("--duration", type=float)
    parser.add_argument("--start-epoch", type=float)
    parser.add_argument("--output")
    parser.add_argument("--mode", choices=("batch", "sequential"),
                        default=os.environ.get("CHANNEL_SCHEDULE_MODE", "batch"))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    schedule = load_json(args.schedule)
    ue_map = load_ue_map(args.ue_map)
    hosts = parse_hosts(args.cell_host)
    events = normalize(schedule, ue_map, hosts, args.duration)
    if args.operation == "check":
        verify = check_batch if args.mode == "batch" else check
        result = verify(events, hosts, args.remote_bin,
                        schedule.get("expected_model_type"))
        print(json.dumps({"checked": len(result), "targets": result}, sort_keys=True))
        return
    if args.start_epoch is None or not args.output:
        raise SystemExit("run requires --start-epoch and --output")
    state = execute(schedule, events, hosts, args.remote_bin,
                    args.start_epoch, Path(args.output), args.mode)
    print(json.dumps({"success": state["success"],
                      "transitions": len(state["transitions"])}, sort_keys=True))


if __name__ == "__main__":
    main()
