#!/usr/bin/env python3
"""Validate and execute a verified RFsim channel schedule over SSH.

The schedule clock is relative to traffic start. Every change is applied by
the cell-side ``channel-cell.py`` helper, which reads the value back before it
reports success. The resulting JSON is therefore suitable as a dataset label:
it contains both the requested transition and the actual remote UTC epoch.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import time


PARAMETERS = {
    "noise_power_dB", "ploss", "riceanf", "aoa", "offset", "forgetf"
}


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


def execute(schedule: dict, events: list[dict], hosts: dict[int, str],
            remote_bin: str, start_epoch: float, output: Path) -> dict:
    state = {
        "schema_version": 1,
        "schedule": schedule,
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
    return parser


def main() -> None:
    args = build_parser().parse_args()
    schedule = load_json(args.schedule)
    ue_map = load_ue_map(args.ue_map)
    hosts = parse_hosts(args.cell_host)
    events = normalize(schedule, ue_map, hosts, args.duration)
    if args.operation == "check":
        result = check(events, hosts, args.remote_bin,
                       schedule.get("expected_model_type"))
        print(json.dumps({"checked": len(result), "targets": result}, sort_keys=True))
        return
    if args.start_epoch is None or not args.output:
        raise SystemExit("run requires --start-epoch and --output")
    state = execute(schedule, events, hosts, args.remote_bin,
                    args.start_epoch, Path(args.output))
    print(json.dumps({"success": state["success"],
                      "transitions": len(state["transitions"])}, sort_keys=True))


if __name__ == "__main__":
    main()
