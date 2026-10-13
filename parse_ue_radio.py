#!/usr/bin/env python3
"""Parse per-second UE serving-cell measurements from OAI container logs."""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path


PREFIX = "UE_RADIO_V1 "
FIELDS = (
    "utc_second", "emitted_epoch_us", "ue", "cell", "ssb", "samples",
    "ss_rsrp_dbm", "ss_rsrq_db", "ss_sinr_db",
)
OUTPUT_FIELDS = (
    "utc_second", "emitted_epoch_us", "ue", "cell", "ue_index", "ssb",
    "samples", "ss_rsrp_dbm", "ss_rsrq_db", "ss_sinr_db",
)


def parse_line(line: str) -> dict | None:
    if PREFIX not in line:
        return None
    values = {}
    for token in line.split(PREFIX, 1)[1].split():
        key, separator, value = token.partition("=")
        if separator:
            values[key] = value
    missing = [field for field in FIELDS if field not in values]
    if missing:
        raise ValueError(f"UE radio record is missing fields: {missing}")
    return {
        "utc_second": int(values["utc_second"]),
        "emitted_epoch_us": int(values["emitted_epoch_us"]),
        "ssb": int(values["ssb"]),
        "samples": int(values["samples"]),
        "ss_rsrp_dbm": float(values["ss_rsrp_dbm"]),
        "ss_rsrq_db": float(values["ss_rsrq_db"]),
        "ss_sinr_db": float(values["ss_sinr_db"]),
    }


def load_mapping(path: Path) -> dict[str, dict]:
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    mapping = {}
    for row in rows:
        name = str(row.get("ue") or "").strip()
        if not name:
            continue
        mapping[name] = {
            "cell": int(row["cell"]),
            "ue_index": int(row["ue_index"]),
        }
    if not mapping:
        raise ValueError("rnti_map.csv contains no UE mappings")
    return mapping


def parse_logs(logs_dir: Path, timing_path: Path) -> list[dict]:
    timing = json.loads(timing_path.read_text())
    start = float(timing["senders_start_epoch"])
    duration = float(timing["duration_s"])
    first_second = math.floor(start)
    end_second = math.ceil(start + duration)
    mapping = load_mapping(logs_dir / "rnti_map.csv")
    rows = []
    for name, identity in sorted(mapping.items()):
        source = logs_dir / f"{name}_radio.log"
        if not source.is_file():
            raise ValueError(f"missing UE radio log: {source.name}")
        for line in source.read_text(errors="replace").splitlines():
            parsed = parse_line(line)
            if parsed is None or not first_second <= parsed["utc_second"] < end_second:
                continue
            parsed.update({"ue": name, **identity})
            rows.append(parsed)
    rows.sort(key=lambda row: (row["utc_second"], row["ue"]))
    keys = [(row["utc_second"], row["ue"]) for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate UE radio rows for the same UTC second")
    expected = max(end_second - first_second, 1)
    minimum = max(expected - max(5, math.ceil(expected * .05)), 1)
    for name in mapping:
        count = sum(row["ue"] == name for row in rows)
        if count < minimum:
            raise ValueError(
                f"{name} has only {count}/{expected} UE radio seconds; minimum is {minimum}"
            )
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows({field: row[field] for field in OUTPUT_FIELDS} for row in rows)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--timing", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = parse_logs(args.logs, args.timing)
    write_csv(args.output, rows)
    print(f"  wrote {len(rows)} UE radio rows to {args.output.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
