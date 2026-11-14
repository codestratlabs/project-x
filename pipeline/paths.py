"""Every folder the pipeline reads or writes, in one place. Each can be moved with an environment variable.

    PX_DATA          raw data root: holds dumps/ (downloaded filings) and input/ (Form D database)   default: the folder above the repo
    PX_DUMPS         downloaded filings and their manifests                                           default: $PX_DATA/dumps
    PX_FINDER_ROOT   where company-finder runs write <run>/out/<company>_*/ (or out/<company>*/)       default: $PX_DATA
    PX_WORK          everything the pipeline builds (never committed)                                  default: <repo>/work
                       work/prepared/   filings lined up with their mentions (pdfs/, companies/)
                       work/inputs/<co>/data/   structured tables (N-PORT price marks, DOL rows)
                       work/out/<co>/   evidence/, indirect runs, located rows, Form D offerings
"""
from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PIPELINE = REPO / "pipeline"
CONFIG = PIPELINE / "config"                      # config/<company>.json: everything company-specific
FINDER = REPO / "company-finder"
ENV_FILE = FINDER / ".env"                         # OPENROUTER_API_KEY, SEC_USER_AGENT (never committed)

DATA = Path(os.environ.get("PX_DATA", REPO.parent))
DUMPS = Path(os.environ.get("PX_DUMPS", DATA / "dumps"))
FORMD_DB = DATA / "input" / "formd" / "formd.duckdb"
FINDER_ROOT = Path(os.environ.get("PX_FINDER_ROOT", DATA))

WORK = Path(os.environ.get("PX_WORK", REPO / "work"))
PREPARED = WORK / "prepared"
INPUTS = WORK / "inputs"
OUT = WORK / "out"


def config_path(company: str) -> Path:
    return CONFIG / f"{company}.json"


def finder_run_dirs(folder: str) -> list[Path]:
    """company-finder output folders for one dump folder: <root>/out/<folder>*/ and <root>/<run>/out/<folder>_*/."""
    return sorted({*FINDER_ROOT.glob(f"out/{folder}"), *FINDER_ROOT.glob(f"out/{folder}_*"), *FINDER_ROOT.glob(f"*/out/{folder}_*")})
