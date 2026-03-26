# Lightweight Trace-Driven Burst Traffic Generation for 5G Network Digital Twins (Paper Artifact)

This repository contains the Jupyter notebooks used to preprocess packet-trace-derived data, learn a burst-level traffic model based on clustering and Markov dynamics, generate synthetic bursts, and export the figures and tables used in the accompanying paper:

**Lightweight Trace-Driven Burst Traffic Generation for 5G Network Digital Twins**  
Ghinwa Ismail, Samir Si-Mohammed, and Fabrice Theoleyre  
*IEEE NetSoft 2026*

The workflow is notebook-first: each application can be processed independently, and a dedicated paper notebook consolidates results for reporting and reproducibility.

---

## Citation

If you use this repository, please cite the accompanying paper:

```bibtex
@inproceedings{ismail2026lightweight,
  title={Lightweight Trace-Driven Burst Traffic Generation for 5G Network Digital Twins},
  author={Ismail, Ghinwa and Si-Mohammed, Samir and Theoleyre, Fabrice},
  booktitle={IEEE Conference on Network Softwarization (NetSoft)},
  year={2026}
}
```
---

## Repository structure

- `notebooks/`  
  Jupyter notebooks for the full pipeline:
  - `Traffic_generator_<app>.ipynb`: per-application pipeline (preprocess → fit → generate → export)
  - `Traffic_generator_paper.ipynb`: paper-oriented exports (figures/tables)

- `data/` *(local only, not versioned)*  
  Place the per-application input files here (see **Data layout** below).

- `artifacts/`  
  Run outputs (models, metrics, figures, tables). Each execution writes into a `run_*` directory for traceability.

- `requirements.txt`  
  Python dependencies used for the experiments.

---

## Setup

Create and activate a virtual environment, then install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
