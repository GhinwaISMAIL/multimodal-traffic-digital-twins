from __future__ import annotations

import copy
import hashlib
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd


SCHEDULE_FILES = ("scenario_schedule.csv", "scenario_schedule.json",
                  "scenario_schedule.sha256.json")
REQUIRED = ("session_id", "experimental_ue", "start_s", "end_s", "traffic_model",
            "source_category", "scenario_role")
BURST_COLUMNS = ("synthetic_flow_id", "synthetic_burst_idx", "on_dur_s",
                 "off_dur_s", "bytes", "packets")


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
                          encoding="utf-8")


def _digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _within(root, relative):
    path = (root / relative).resolve()
    path.relative_to(root.resolve())
    return path


def _verify(root, name):
    checksums = _json(root / name)
    if not isinstance(checksums, dict) or not checksums:
        raise ValueError(f"Invalid checksum manifest: {name}")
    for relative, digest in checksums.items():
        if _digest(_within(root, relative)) != digest:
            raise ValueError(f"Checksum mismatch: {relative}")


def _checksums(root, files, name):
    _write_json(root / name, {p.relative_to(root).as_posix(): _digest(p) for p in files})


def load_request(root, cfg):
    root = Path(root).resolve()
    cfg = copy.deepcopy(cfg)
    if cfg.get("scenario_source") != "measurement_driven":
        raise ValueError("Expected scenario_source: measurement_driven")
    spec = cfg["measurement_schedule"]
    if (spec.get("schema_version") != 1 or spec.get("time_reference") != "scenario_start"
            or spec.get("allow_overlapping_sessions") is not True
            or spec.get("byte_policy") != "reference_only"
            or spec.get("sampling_policy") != "one_flow_per_session"):
        raise ValueError("Unsupported measurement schedule policy")
    source = _within(root, spec["path"])
    if source.name != SCHEDULE_FILES[0]:
        raise ValueError("Expected the normalized scenario_schedule.csv")
    _verify(source.parent, SCHEDULE_FILES[2])
    metadata = _json(source.parent / SCHEDULE_FILES[1])
    if (metadata.get("schema_version") != 1 or metadata.get("scenario_mode") != "schedule"
            or metadata.get("time_origin") != "scenario_start"):
        raise ValueError("Unsupported frozen schedule metadata")
    if _digest(source) != spec["sha256"] or spec["sha256"] != metadata["normalized_sha256"]:
        raise ValueError("Schedule checksum disagrees with scenario configuration")
    duration = float(cfg["simulation"]["duration"])
    if duration != 86400 or duration != metadata["logical_duration_s"]:
        raise ValueError("Measurement scenarios require the frozen 86400-second logical day")
    population = metadata["ue_population"]
    if (not isinstance(population, list) or not population
            or any(not isinstance(name, str) or not name.strip() for name in population)
            or len(set(population)) != len(population)):
        raise ValueError("Expected a nonempty list of distinct experimental UEs")
    ue_count = len(population)
    expected = {name: f"ue{index}" for index, name in enumerate(population, 1)}
    if spec["ue_mapping"] != expected:
        raise ValueError("UE mapping must preserve the frozen population order")
    sim = cfg["simulation"]
    if (sim["n_ue"], sim["num_cells"], sim["ues_per_cell"]) != (ue_count, 1, ue_count):
        raise ValueError("Measurement topology must match the frozen UE population in one cell")
    seed = sim["random_seed"]
    if not isinstance(seed, int) or not 0 <= seed < 2**32:
        raise ValueError("Invalid random seed")
    frame = pd.read_csv(source, dtype=str, keep_default_na=False)
    if frame.empty or not set(REQUIRED) <= set(frame):
        raise ValueError("Missing schedule sessions or required columns")
    for column in REQUIRED:
        if frame[column].str.strip().eq("").any():
            raise ValueError(f"Empty required field: {column}")
    for column in ("start_s", "end_s"):
        frame[column] = pd.to_numeric(frame[column], errors="raise")
    if (not np.isfinite(frame[["start_s", "end_s"]].to_numpy()).all()
            or not ((frame.start_s >= 0) & (frame.start_s < frame.end_s)
                    & (frame.end_s <= duration)).all()):
        raise ValueError("Session times must satisfy 0 <= start_s < end_s <= 86400")
    if frame.session_id.duplicated().any():
        raise ValueError("Session IDs must be unique")
    if not set(frame.experimental_ue) <= set(population):
        raise ValueError("Unknown experimental UE")
    if not set(frame.scenario_role) <= {"baseline", "augmented"}:
        raise ValueError("Unknown scenario role")
    apps = sorted(frame.traffic_model.unique())
    if apps != sorted(cfg["apps"]) or set(apps) != set(spec["artifacts"]):
        raise ValueError("Configured apps must match the scheduled apps and artifact registry")
    network = cfg["network"]
    ipaddress.IPv4Address(network["dn_ip"])
    for index in range(ue_count):
        ipaddress.IPv4Address(f"{network['ue_ip_prefix']}{int(network['ue_ip_start']) + index}")
    for key in ("dl_port", "ul_port"):
        if not isinstance(network[key], int) or not 1 <= network[key] <= 65535:
            raise ValueError(f"Invalid network {key}")
    tc = cfg["temporal_correlation"]
    for key in ("rtt_delay_range", "dl_bursts_per_ul_request"):
        low, high = tc[key]
        if not math.isfinite(low) or not math.isfinite(high) or low < 0 or low > high:
            raise ValueError(f"Invalid {key}")
    if any(int(v) != v or v < 1 for v in tc["dl_bursts_per_ul_request"]):
        raise ValueError("DL bursts per request must be positive integers")
    if not math.isfinite(tc["jitter"]) or tc["jitter"] < 0:
        raise ValueError("Invalid timing jitter")
    output = spec.pop("output_run")
    digest = hashlib.sha256(json.dumps(cfg, sort_keys=True, allow_nan=False).encode()).hexdigest()
    spec["output_run"] = output
    if output != f"traffic_profiles/run_measurement_{digest[:20]}":
        raise ValueError("Output run does not match the frozen generation configuration")
    return {"root": root, "config": cfg, "source": source, "metadata": metadata,
            "sessions": frame.sort_values(["start_s", "experimental_ue", "session_id"], kind="stable"),
            "output": _within(root, output), "request_sha256": digest}


def load_pools(request):
    result = {}
    registry = request["config"]["measurement_schedule"]["artifacts"]
    for app, directions in registry.items():
        if set(directions) != {"downlink", "uplink"}:
            raise ValueError(f"{app}: both directional artifacts are required")
        result[app] = {}
        for name, reference in directions.items():
            path = _within(request["root"], reference["path"])
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != reference["sha256"]:
                raise ValueError(f"Artifact changed since scenario preparation: {app}/{name}")
            frame = pd.read_parquet(io.BytesIO(content))
            if frame.empty or not set(BURST_COLUMNS) <= set(frame):
                raise ValueError(f"{app}/{name}: missing burst data")
            numeric = frame[list(BURST_COLUMNS)].apply(pd.to_numeric, errors="raise")
            if (not np.isfinite(numeric.to_numpy()).all() or (numeric.on_dur_s <= 0).any()
                    or (numeric.off_dur_s < 0).any() or (numeric.bytes <= 0).any()
                    or (numeric.packets < 1).any()
                    or (numeric.synthetic_flow_id % 1 != 0).any()):
                raise ValueError(f"{app}/{name}: invalid burst values")
            for column in BURST_COLUMNS:
                frame[column] = numeric[column]
            if ("packets_export" in frame) != ("pkt_size_export" in frame):
                raise ValueError(f"{app}/{name}: incomplete packet projection")
            result[app]["dl" if name == "downlink" else "ul"] = frame
    return result


def _packet_projection(frame):
    frame = frame.copy()
    projected = "packets_export" in frame
    counts, sizes = [], []
    for row in frame.itertuples(index=False):
        if projected:
            count, size = float(row.packets_export), float(row.pkt_size_export)
            if (not math.isfinite(count) or not math.isfinite(size) or count < 1
                    or int(count) != count or int(size) != size or not 64 <= size <= 1472):
                raise ValueError("Invalid projected packet count or payload size")
            count, size = int(count), int(size)
        else:
            count = max(1, int(row.packets), math.ceil(float(row.bytes) / 1472))
            size = max(64, min(1472, math.ceil(float(row.bytes) / count)))
        counts.append(count)
        sizes.append(size)
    frame["packets_export"] = counts
    frame["pkt_size_export"] = sizes
    if "on_dur_s" in frame:
        frame["source_on_dur_s"] = frame.on_dur_s
    frame["on_dur_s"] = frame.absolute_end_time - frame.absolute_start_time
    if "source_on_dur_s" in frame:
        frame["time_scale"] = frame.on_dur_s / frame.source_on_dur_s
    frame["pps"] = frame.packets_export / frame.on_dur_s
    frame["generated_bytes"] = frame.packets_export * frame.pkt_size_export
    return frame


def _existing(request):
    target = request["output"]
    config = _json(target / "config.json")
    if config.get("request_sha256") != request["request_sha256"]:
        raise ValueError("Existing run belongs to a different generation request")
    if _json(target / "generation_config.json") != request["config"]:
        raise ValueError("Existing generation configuration has changed")
    if config.get("generation_status") not in ("bursts_ready", "complete"):
        raise ValueError("Existing generation run is incomplete")
    _verify(target, "burst_checksums.json")
    if config["generation_status"] == "complete":
        _verify(target, "mgen_checksums.json")
    return config


def generate_bursts(request, pools, app_modes, aligners, parameters):
    target = request["output"]
    if target.exists():
        _existing(request)
        return target
    cfg = request["config"]
    mapping = cfg["measurement_schedule"]["ue_mapping"]
    frames = {}
    next_flow, next_batch = 1, 0
    for row in request["sessions"].to_dict("records"):
        app, sid = row["traffic_model"], row["session_id"]
        seed_bytes = json.dumps([cfg["simulation"]["random_seed"], sid]).encode()
        seed = int.from_bytes(hashlib.sha256(seed_bytes).digest()[:4], "big")
        rng = np.random.default_rng(seed)
        selected = []
        for direction in ("dl", "ul"):
            pool = pools[app][direction]
            flow = rng.choice(sorted(pool.synthetic_flow_id.unique()))
            selected.append(pool.loc[pool.synthetic_flow_id.eq(flow)].copy().sort_values("synthetic_burst_idx"))
        mode = app_modes[app][0]
        state = np.random.get_state()
        try:
            np.random.seed(seed)
            dl, ul = aligners[mode](*selected, row["end_s"] - row["start_s"], parameters)
        finally:
            np.random.set_state(state)
        batch_ids = sorted({int(v) for frame in (dl, ul) if "correlated_batch_id" in frame
                            for v in frame.correlated_batch_id.dropna()})
        batch_map = {value: next_batch + index for index, value in enumerate(batch_ids)}
        next_batch += len(batch_map)
        for direction, frame in (("dl", dl), ("ul", ul)):
            if frame.empty:
                raise ValueError(f"{sid}: timing model produced no {direction} bursts")
            frame = frame.copy().reset_index(drop=True)
            frame["absolute_start_time"] += row["start_s"]
            frame["absolute_end_time"] += row["start_s"]
            if "flow_start_time" in frame:
                frame["flow_start_time"] += row["start_s"]
            times = frame[["absolute_start_time", "absolute_end_time"]]
            if (not np.isfinite(times.to_numpy()).all()
                    or (frame.absolute_start_time < row["start_s"]).any()
                    or (frame.absolute_end_time > row["end_s"]).any()
                    or (frame.absolute_end_time <= frame.absolute_start_time).any()):
                raise ValueError(f"{sid}: generated bursts exceed the scheduled session")
            frame["source_synthetic_flow_id"] = frame.synthetic_flow_id
            frame["synthetic_flow_id"] = next_flow
            next_flow += 1
            if "correlated_batch_id" in frame:
                frame["correlated_batch_id"] = frame.correlated_batch_id.map(batch_map).astype("Int64")
            if "timeline_flow_id" in frame:
                frame["timeline_flow_id"] = sid + ":" + frame.timeline_flow_id.astype(str)
            for column in REQUIRED:
                frame[column] = row[column]
            for column in ("source_session_id", "source_traffic_bytes", "source_traffic_model"):
                frame[column] = row.get(column, "")
            frame["source_provenance"] = json.dumps(row, sort_keys=True)
            frame["ue_name"] = mapping[row["experimental_ue"]]
            frame["app"] = app
            frame["direction"] = direction
            frame = _packet_projection(frame)
            frames.setdefault((frame.ue_name.iloc[0], app, direction), []).append(frame)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".measurement-", dir=target.parent) as temporary:
        stage = Path(temporary)
        for name in SCHEDULE_FILES:
            (stage / name).write_bytes((request["source"].parent / name).read_bytes())
        _write_json(stage / "generation_config.json", cfg)
        burst_dir = stage / "bursts"
        burst_dir.mkdir()
        for (ue, app, direction), chunks in sorted(frames.items()):
            pd.concat(chunks, ignore_index=True).sort_values(
                ["absolute_start_time", "session_id", "synthetic_burst_idx"], kind="stable"
            ).to_parquet(burst_dir / f"{ue}_{app}_{direction}_bursts.parquet", index=False)
        identities = [{"experimental_ue": name, "ue_name": ue, "ue_class": "measurement"}
                      for name, ue in mapping.items()]
        pd.DataFrame(identities).to_csv(stage / "ue_mapping.csv", index=False)
        pd.DataFrame(identities).to_csv(stage / "ue_flow_assignments.csv", index=False)
        config = {
            "scenario_mode": "schedule", "scenario_source": "measurement_driven",
            "generation_status": "bursts_ready", "request_sha256": request["request_sha256"],
            "run_name": target.name, "simulation_duration": cfg["simulation"]["duration"],
            "logical_duration_s": cfg["simulation"]["duration"], "n_ue": len(mapping),
            "apps": cfg["apps"], "network": cfg["network"],
            "random_seed": cfg["simulation"]["random_seed"],
            "user_class_mode": "measurement", "profiles": [],
            "sampling_strategy": "one_flow_per_session", "byte_policy": "reference_only",
            "temporal_correlation": parameters,
            "app_traffic_modes": {app: mode[0] for app, mode in app_modes.items()},
            "validation": {"passed": True, "errors": [], "warnings": []},
        }
        _checksums(stage, [p for p in stage.rglob("*") if p.is_file()], "burst_checksums.json")
        _write_json(stage / "config.json", config)
        stage.rename(target)
    return target


def _event_text(events):
    return "".join(text + "\n" for _, _, text in sorted(events))


def export_mgen(request):
    target = request["output"]
    config = _existing(request)
    if config["generation_status"] == "complete":
        return target
    cfg = request["config"]
    net = cfg["network"]
    mapping = cfg["measurement_schedule"]["ue_mapping"]
    session_indices = {sid: index for index, sid in enumerate(request["sessions"].session_id)}
    if len(session_indices) > 64512:
        raise ValueError("Too many sessions for distinct UDP source ports")
    flow_id = 0
    manifests, flow_map, designed = [], [], []
    dl_events, offered = [], []
    target_scripts = target / "mgen_scripts"
    if target_scripts.exists():
        raise ValueError("Unfinished MGEN output already exists; inspect it before retrying")
    with tempfile.TemporaryDirectory(prefix=".mgen-", dir=target) as temporary:
        stage = Path(temporary)
        for index, (experimental, ue) in enumerate(mapping.items()):
            ue_ip = f"{net['ue_ip_prefix']}{int(net['ue_ip_start']) + index}"
            ul_events, ue_dl = [], []
            for app in cfg["apps"]:
                for direction in ("dl", "ul"):
                    path = target / "bursts" / f"{ue}_{app}_{direction}_bursts.parquet"
                    if not path.exists():
                        continue
                    frame = pd.read_parquet(path)
                    for burst in frame.to_dict("records"):
                        flow_id += 1
                        if flow_id >= 2**32:
                            raise ValueError("MGEN flow ID space exhausted")
                        start = math.ceil(burst["absolute_start_time"] * 1e6) / 1e6
                        end = math.floor(burst["absolute_end_time"] * 1e6) / 1e6
                        if end <= start:
                            raise ValueError("Burst duration is below MGEN timestamp precision")
                        packets, size = int(burst["packets_export"]), int(burst["pkt_size_export"])
                        pps = float(f"{packets / (end - start):.12f}")
                        dst = ue_ip if direction == "dl" else net["dn_ip"]
                        port = net["dl_port"] if direction == "dl" else net["ul_port"]
                        src = 1024 + session_indices[burst["session_id"]]
                        events = ue_dl if direction == "dl" else ul_events
                        events.extend([
                            (start, 1, f"{start:.6f} ON {flow_id} UDP SRC {src} DST {dst}/{port} "
                             f"PERIODIC [{pps:.12f} {size}]"),
                            (end, 0, f"{end:.6f} OFF {flow_id}"),
                        ])
                        provenance = {key: burst.get(key, "") for key in (
                            "session_id", "experimental_ue", "scenario_role", "source_category",
                            "source_session_id", "source_traffic_bytes", "source_traffic_model",
                            "source_provenance", "correlated_batch_id", "timeline_flow_id")}
                        flow_map.append({**provenance, "unique_flow_id": flow_id, "ue_name": ue,
                                         "app": app, "direction": direction,
                                         "synthetic_flow_id": burst["synthetic_flow_id"]})
                        nbytes = pps * (end - start) * size
                        offered.append({"session_id": burst["session_id"], "direction": direction,
                                        "offered_bytes": nbytes, "offered_packets": pps * (end - start)})
                        designed.append({"run_id": target.name, "ue": ue, "ue_class": "measurement",
                                         "flow_id": flow_id, "direction": direction, "app": app,
                                         "proj_packets": round(pps * (end - start)), "proj_bytes": round(nbytes),
                                         "proj_duration_s": config["simulation_duration"],
                                         "proj_throughput_bps": nbytes * 8 / config["simulation_duration"]})
            (stage / f"{ue}_ul_tx.mgn").write_text(_event_text(ul_events))
            rx_end = max((time for time, _, _ in ue_dl), default=0) + 15
            (stage / f"{ue}_dl_rx.mgn").write_text(
                f"0.0 LISTEN UDP {net['dl_port']}\n{rx_end:.6f} IGNORE UDP {net['dl_port']}\n")
            manifests.append({"ue_name": ue, "ue_ip": ue_ip, "ue_class": "measurement",
                              "experimental_ue": experimental, "dl_rx_script": f"{ue}_dl_rx.mgn",
                              "ul_tx_script": f"{ue}_ul_tx.mgn", "n_dl_events": len(ue_dl),
                              "n_ul_events": len(ul_events)})
            dl_events.extend(ue_dl)
        (stage / "dn_dl_tx.mgn").write_text(_event_text(dl_events))
        last_ul = max((row["end_s"] for row in request["sessions"].to_dict("records")), default=0) + 15
        (stage / "dn_ul_rx.mgn").write_text(
            f"0.0 LISTEN UDP {net['ul_port']}\n{last_ul:.6f} IGNORE UDP {net['ul_port']}\n")
        pd.DataFrame(manifests).to_csv(stage / "manifest.csv", index=False)
        pd.DataFrame(flow_map).to_csv(stage / "flow_batch_map.csv", index=False)
        pd.DataFrame(designed).to_parquet(stage / "designed_kpis.parquet", index=False)
        totals = pd.DataFrame(offered).pivot_table(index="session_id", columns="direction",
                                                 values="offered_bytes", aggfunc="sum", fill_value=0)
        totals = totals.rename(columns={"dl": "generated_dl_bytes", "ul": "generated_ul_bytes"})
        report = request["sessions"].merge(totals, on="session_id", validate="one_to_one")
        report["generated_total_bytes"] = report.generated_dl_bytes + report.generated_ul_bytes
        report["byte_policy"] = "reference_only"
        report.to_csv(stage / "session_generation.csv", index=False)
        stage.rename(target_scripts)
    os.replace(target_scripts / "designed_kpis.parquet", target / "designed_kpis.parquet")
    os.replace(target_scripts / "session_generation.csv", target / "session_generation.csv")
    _checksums(target, [*target_scripts.iterdir(), target / "designed_kpis.parquet",
                       target / "session_generation.csv"], "mgen_checksums.json")
    config["generation_status"] = "complete"
    pending = target / ".config.json.tmp"
    _write_json(pending, config)
    os.replace(pending, target / "config.json")
    return target
