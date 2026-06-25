# MGEN Multi-App 5G Traffic Scenario Builder

Generates realistic multi-application, multi-UE synthetic traffic profiles
and replays them over a live OAI 5G testbed using MGEN.

---

## Pipeline

```
Notebook 0  →  audit & project burst pools
Notebook 1  →  build multi-UE scenario        (reads scenario_config.yaml)
Notebook 2  →  export MGEN scripts
Notebook 3  →  deploy to physical testbed     (reads testbed_config.yaml)
```

Run them in order. Each notebook validates its inputs before proceeding.

---

## Setup

**1. Install dependencies**
```bash
pip install pandas numpy pyarrow pyyaml
```

**2. Configure the scenario** — edit `scenario_config.yaml` at the repo root.

**3. Configure the testbed** — copy the example and fill in your values:
```bash
cp testbed_config.example.yaml testbed_config.yaml
```
`testbed_config.yaml` is gitignored — it contains real SSH hosts and IPs.

---

## Supported apps

`aparat`, `filimo`, `igap`, `telegram`, `youtube`

To add a new app, place its Markov burst parquets under
`artifacts/<app>/downlink/` and `artifacts/<app>/uplink/`,
then add it to `apps` in `scenario_config.yaml`.

---

## Adapting to a different testbed

Only `testbed_config.yaml` needs to change.
Notebooks 0–2 are fully testbed-agnostic.

---

## Dependencies

```
Python ≥ 3.9  |  pandas  |  numpy  |  pyarrow  |  pyyaml  |  MGEN
```

## Deployment (Notebook 3 — profile-aware)

Notebook 3 reads `testbed_config.yaml` (written by the dashboard's Testbed page)
and detects the deployment profile automatically — no manual edits:

- **`cots_physical`** — substitutes static physical UE IPs and emits a per-box
  SSH/SCP command guide.
- **`powder_rfsim_docker`** — emits `deploy_rfsim.sh`, which at deploy time
  resolves each UE's live PDU IP from `oaitun_ue1`, rewrites the DN downlink
  with those IPs, re-asserts the data-plane route, copies scripts into the DN
  and UE containers, and starts receivers then senders.

`testbed_config.yaml` is gitignored (it holds live, per-experiment values such
as the POWDER node FQDN). Copy `testbed_config.example.yaml` and fill it in, or
let the dashboard write it.
