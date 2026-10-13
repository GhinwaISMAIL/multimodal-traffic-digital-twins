# MGEN Multi-App 5G Traffic Scenario Builder

Generates realistic multi-application, multi-UE synthetic traffic profiles and
replays them over a live OAI 5G testbed using MGEN. Traffic behaviour is learned
from real application traces, so the injected load mimics real apps rather than
constant-bit-rate filler.

## Pipeline

```
Notebook 0  →  audit & project burst pools
Notebook 1  →  build multi-UE scenario        (reads  scenario_config.yaml)
Notebook 2  →  export MGEN scripts             (writes mgen_scripts/)
Notebook 3  →  deploy to a testbed             (reads  testbed_config.yaml)
```

Run them in order — each notebook validates its inputs before proceeding.
Configs can be edited by hand or written by the companion dashboard.

- **Notebook 0** audits the per-app burst pools under `artifacts/` and projects
  what a run would contain.
- **Notebook 1** builds a multi-UE scenario: it samples flows per UE, applies
  request–response temporal correlation, and writes per-UE time-aligned burst
  tables plus a `config.json`. It optionally accepts **named profiles** (see
  below) to control traffic composition.
- **Notebook 2** converts a run's bursts into MGEN `ON`/`OFF` event scripts
  (`.mgn`) plus a `flow_batch_map.csv` linking request flows to their responses.
  Receivers terminate cleanly via an `IGNORE` event after the last burst.
- **Notebook 3** reads `testbed_config.yaml`, detects the deployment profile,
  and emits the matching deployment package (see **Deployment** below).

## Setup

1. Install dependencies

```
pip install pandas numpy pyarrow pyyaml
```

2. Configure the scenario — edit `scenario_config.yaml` at the repo root
   (or use the dashboard's Design page).
3. Configure the testbed — copy the example and fill in your values
   (or use the dashboard's Testbed page):

```
cp testbed_config.example.yaml testbed_config.yaml
```

`testbed_config.yaml` is gitignored — it holds live, per-experiment SSH hosts
and IPs (e.g. the POWDER node FQDN, which changes every instantiation).

## Profiles — controlling traffic composition

By default, Notebook 1 distributes apps across UEs using learned user classes
(`heavy` / `medium` / `light`). To control composition explicitly, add a
`profiles` block to `scenario_config.yaml`:

```yaml
profiles:
  - name: video_heavy
    count: 4              # how many UEs get this profile
    base: heavy          # borrows a learned class's realism (DL/UL split, etc.)
    flows: 80            # per-UE flow target
    app_mix: {youtube: 50, filimo: 40, telegram: 10}   # weights; 0 excludes an app
```

Profile counts must sum to `n_ue`. Profiles change *which* apps run on *which*
UEs and *how many* flows — not the learned burst shapes (those still come from
the real traces via `base`). With no `profiles` block, the user-class
distribution is used instead.

## Supported apps

`aparat`, `filimo`, `igap`, `telegram`, `youtube`

To add a new app, place its Markov burst parquets under
`artifacts/<app>/downlink/` and `artifacts/<app>/uplink/` (produced by the
per-app `Traffic_generator_<app>.ipynb` notebooks), then add it to `apps` in
`scenario_config.yaml`.

## Deployment (Notebook 3 — profile-aware)

Notebook 3 reads `testbed_config.yaml` and detects the deployment profile
automatically — no manual edits:

- **`cots_physical`** — substitutes static physical UE IPs and emits a per-box
  SSH/SCP command guide. Each UE is a separate physical host with a known IP.
- **`powder_rfsim_docker`** — emits `deploy_rfsim.sh`, which at deploy time
  resolves each UE's live PDU IP from `oaitun_ue1`, rewrites the DN downlink
  with those IPs, re-asserts the data-plane route, copies scripts into the DN
  and UE containers, and starts receivers then senders. UEs are containers on
  one POWDER node with dynamic IPs.

The generated package lands in each run's `deployment/` folder. For RFsim, run
the emitted script:

```
bash traffic_profiles/run_<…>/deployment/deploy_rfsim.sh
```

It aborts if any UE's tunnel is down (no `oaitun_ue1` address), so a missing
attach is caught before traffic is injected.

## Adapting to a different testbed

Only `testbed_config.yaml` changes — Notebooks 0–3 are all testbed-agnostic.
A new deployment *type* (beyond `cots_physical` / `powder_rfsim_docker`) needs a
matching adapter branch in Notebook 3's Cell 2.

## Repository layout

```
artifacts/            per-app Markov burst pools (input)  — <app>/{downlink,uplink}/
traffic_profiles/     generated runs (gitignored)         — run_<…>/{bursts,mgen_scripts,deployment}
notebooks/            the 0–3 pipeline + per-app generators
scenario_config.yaml  traffic model: apps, UEs, profiles, duration, correlation
testbed_config.yaml   deployment endpoints (gitignored; see .example)
```

## Companion dashboard

A separate Streamlit dashboard (`twindash`) can author both configs (Design and
Testbed pages) and view results — designed-vs-realized RTT, request→response
coupling, and per-flow sent/received — by reading each run's logs.

## Dependencies

```
Python ≥ 3.9  |  pandas  |  numpy  |  pyarrow  |  pyyaml  |  MGEN
```

MGEN must be installed on every UE/DN (for containers, inside the image).


## Measurement-driven scenarios

TwinDash can write `scenario_source: measurement_driven` with a pointer to a
validated, frozen `scenario_schedule.csv`. Use **Write measurement-driven
scenario** on the schedule page, then run all cells in `build_scenario_1.ipynb`
and `mgen_script_2.ipynb`, in that order. Missing `scenario_source` preserves the
original profile workflow; unknown modes are rejected.

The configuration pins both directional burst artifacts by path and SHA-256,
a seed, and the mapping from the supplied experimental identifiers to deployment UE
names. The existing application alignment functions run separately for each
session. One learned flow is sampled per session and direction; request/response
models retain their UL/DL coupling. Burst times stay inside the session bounds,
and packet rates are recalculated after fitting the timeline. Single-session
sampling does not imply continuous traffic throughout its scheduled interval.

The initial byte policy is `reference_only`: `source_traffic_bytes` is preserved
for comparison and does not force generated packet volumes. Input artifacts
must have finite positive ON durations, nonnegative gaps, and valid packet
counts. Prepare corrected artifacts if these checks fail; generation does not
silently repair source model data.

Generated runs use `traffic_profiles/run_measurement_<id>/`. The directory
contains an unchanged copy of the frozen schedule and metadata,
`generation_config.json`, `ue_mapping.csv`, `ue_flow_assignments.csv`, per-UE/app
burst tables, and the standard `mgen_scripts/` files. The MGEN flow map retains
session IDs, source-session references, roles, and provenance. All population members receive
script/manifest entries, including population members with no scheduled traffic.

`session_generation.csv` records generated DL/UL offered bytes beside source
fields. `designed_kpis.parquet` contains per-flow offered load calculated from
exported events. These describe the configured sender load; measured traffic
requires deployment and log collection.

A run is `bursts_ready` after Notebook 1 and becomes `complete` only after
Notebook 2 writes the MGEN outputs and checksums. Pending runs are excluded from
TwinDash deployment. Repeated generation requests reuse verified output;
changing the seed, configuration, or artifact checksums creates a separate run.
Profile-mode Notebook 2 excludes schedule folders from its automatic selection.

Run the offline notebook integration tests with Python containing NumPy,
pandas, PyArrow, PyYAML, and pytest:

```bash
python -m pytest -q tests/test_measurement_generation.py
```

The fixtures exercise overlapping sessions, inactive UEs, input/artifact
checksums, output provenance, repeat generation, and the original profile path.
No radio replay or augmentation is performed by this bridge.
