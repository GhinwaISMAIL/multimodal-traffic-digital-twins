#!/usr/bin/env python3
"""Synchronize and validate clocks used by a distributed traffic run."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import re
import shlex
import subprocess
import time
from pathlib import Path


@dataclass(frozen=True)
class Peer:
    host: str
    server: str
    reach: int
    offset_ms: float
    jitter_ms: float


def parse_nodes(values: list[str]) -> dict[str, str]:
    nodes: dict[str, str] = {}
    for value in values:
        name, separator, host = value.partition("=")
        if not separator or not name or not host:
            raise ValueError(f"invalid --node value: {value!r}")
        if name in nodes:
            raise ValueError(f"duplicate node name: {name}")
        nodes[name] = host
    if len(nodes) < 2:
        raise ValueError("clock validation requires at least two nodes")
    return nodes


def parse_ntpq(output: str, host: str) -> Peer:
    selected = next(
        (line for line in output.splitlines() if line.startswith("*")), None
    )
    if selected is None:
        raise ValueError(f"{host} has no selected NTP peer")
    fields = selected.split()
    if len(fields) < 10:
        raise ValueError(f"cannot parse selected NTP peer on {host}: {selected}")
    try:
        return Peer(
            host=host,
            server=fields[0].removeprefix("*"),
            reach=int(fields[6], 8),
            offset_ms=float(fields[8]),
            jitter_ms=float(fields[9]),
        )
    except ValueError as exc:
        raise ValueError(
            f"cannot parse selected NTP peer on {host}: {selected}"
        ) from exc


def ssh(host: str, remote: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=12",
            host,
            remote,
        ],
        check=True,
        text=True,
        capture_output=True,
    )


def sample(host: str) -> Peer:
    return parse_ntpq(ssh(host, "ntpq -pn").stdout, host)


def synchronize(host: str, server: str) -> str:
    script = (
        "systemctl stop ntp; "
        "rc=0; "
        f"ntpdate -b {shlex.quote(server)} || rc=$?; "
        "systemctl start ntp; "
        "exit $rc"
    )
    result = ssh(host, f"sudo bash -lc {shlex.quote(script)}")
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout).strip()


def gate(
    peers: dict[str, Peer],
    *,
    max_abs_offset_ms: float,
    max_offset_spread_ms: float,
    max_jitter_ms: float,
    enforce_jitter: bool = True,
) -> dict:
    if not peers:
        return {"passed": False, "error": "no NTP samples"}
    offsets = [peer.offset_ms for peer in peers.values()]
    jitters = [peer.jitter_ms for peer in peers.values()]
    reach_ok = all(peer.reach > 0 for peer in peers.values())
    max_abs_offset = max(abs(value) for value in offsets)
    offset_spread = max(offsets) - min(offsets)
    max_jitter = max(jitters)
    offset_ok = (
        max_abs_offset <= max_abs_offset_ms
        and offset_spread <= max_offset_spread_ms
    )
    jitter_ok = max_jitter <= max_jitter_ms
    return {
        "passed": bool(
            reach_ok
            and offset_ok
            and (jitter_ok or not enforce_jitter)
        ),
        "reach_ok": reach_ok,
        "offset_ok": offset_ok,
        "jitter_ok": jitter_ok,
        "jitter_policy": "required" if enforce_jitter else "warning",
        "max_abs_offset_ms": max_abs_offset,
        "offset_spread_ms": offset_spread,
        "max_jitter_ms": max_jitter,
    }


def collect(nodes: dict[str, str]) -> tuple[dict[str, Peer], dict[str, str]]:
    peers: dict[str, Peer] = {}
    errors: dict[str, str] = {}
    for name, host in nodes.items():
        try:
            peers[name] = sample(host)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            errors[name] = str(exc)
    return peers, errors


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def run(args: argparse.Namespace) -> dict:
    nodes = parse_nodes(args.node)
    enforce_jitter = getattr(args, "jitter_policy", "required") == "required"
    thresholds = {
        "max_abs_offset_ms": args.max_abs_offset_ms,
        "max_offset_spread_ms": args.max_offset_spread_ms,
        "max_jitter_ms": args.max_jitter_ms,
    }
    before, before_errors = collect(nodes)
    document = {
        "schema_version": 1,
        "mode": args.operation,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "server": args.server,
        "thresholds": thresholds,
        "nodes": {
            name: {
                "host": host,
                "action": "none",
                "before": asdict(before[name]) if name in before else None,
                "before_error": before_errors.get(name),
            }
            for name, host in nodes.items()
        },
    }

    before_gate = gate(before, enforce_jitter=enforce_jitter, **thresholds)
    if args.operation == "prepare" and (
        before_errors or not before_gate["passed"]
    ):
        targets = list(before_errors)
        targets.extend(
            name
            for name, peer in before.items()
            if (
                peer.reach == 0
                or abs(peer.offset_ms) > args.max_abs_offset_ms
                or peer.jitter_ms > args.max_jitter_ms
            )
        )
        targets = list(dict.fromkeys(targets))
        if not targets and (
            before_gate.get("offset_spread_ms", 0) > args.max_offset_spread_ms
        ):
            targets = list(nodes)
        for name in targets:
            document["nodes"][name]["action"] = "synchronized"
            document["nodes"][name]["sync_output"] = synchronize(
                nodes[name], args.server
            )

        deadline = time.monotonic() + args.timeout_s
        after: dict[str, Peer] = {}
        after_errors: dict[str, str] = {}
        while time.monotonic() < deadline:
            after, after_errors = collect(nodes)
            if not after_errors and gate(
                after, enforce_jitter=enforce_jitter, **thresholds
            )["passed"]:
                break
            time.sleep(args.poll_interval_s)
    else:
        after, after_errors = before, before_errors

    after_gate = gate(after, enforce_jitter=enforce_jitter, **thresholds)
    for name in nodes:
        document["nodes"][name]["after"] = (
            asdict(after[name]) if name in after else None
        )
        document["nodes"][name]["after_error"] = after_errors.get(name)
    document["gate"] = after_gate
    document["passed"] = bool(not after_errors and after_gate["passed"])
    if not after_gate.get("jitter_ok", True) and not enforce_jitter:
        warning = "NTP peer jitter exceeded its diagnostic threshold"
        if after_gate.get("offset_ok"):
            warning += "; current offsets and cross-node spread remained within limits"
        document["warnings"] = [warning]
    if not document["passed"]:
        details = ", ".join(
            f"{name}: {message}" for name, message in after_errors.items()
        )
        document["error"] = details or "clock thresholds were not satisfied"
    return document


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    root.add_argument("operation", choices=("prepare", "verify"))
    root.add_argument("--node", action="append", required=True)
    root.add_argument("--output", required=True)
    root.add_argument("--server", default="155.98.33.74")
    root.add_argument("--max-abs-offset-ms", type=float, default=5.0)
    root.add_argument("--max-offset-spread-ms", type=float, default=5.0)
    root.add_argument("--max-jitter-ms", type=float, default=1.0)
    root.add_argument(
        "--jitter-policy", choices=("required", "warning"), default="required"
    )
    root.add_argument("--timeout-s", type=float, default=90.0)
    root.add_argument("--poll-interval-s", type=float, default=2.0)
    return root


def main() -> None:
    args = parser().parse_args()
    output = Path(args.output)
    try:
        document = run(args)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        document = {
            "schema_version": 1,
            "mode": args.operation,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "passed": False,
            "error": str(exc),
        }
    atomic_json(output, document)
    print(json.dumps(document, sort_keys=True))
    if not document.get("passed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
