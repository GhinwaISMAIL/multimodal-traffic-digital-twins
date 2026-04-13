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
