"""A real run of the aliases and records tasks for Lungpacer: the real pipeline scripts against moto S3.

Opt-in: it needs a Python with the pipeline's dependencies and the Lungpacer reference data.

    PX_TEST_PIPELINE_PYTHON=<company-finder/.venv/bin/python> \
    PX_TEST_FORMD_DB=<.../formd.duckdb> PX_TEST_DOL_DIR=<.../employee_docs/raw> \
    [PX_TEST_NPORT_CSV=<.../lungpacer_nport/nport_holdings.csv>] [PX_TEST_EXPECT_WORK=<work folder of a local run>] \
    python -m pytest tests/test_real_lungpacer.py -s

pipeline/ and tools/ are copied to a temporary repo (PX_REPO), so the config the runner writes never lands in the
checkout, and no company-finder/.env is in reach. The data files are copied before they are seeded, and checked
unchanged afterwards. With PX_TEST_EXPECT_WORK the records are compared byte for byte with that earlier run.
"""
import csv
import hashlib
import io
import json
import os
import shutil
from pathlib import Path

import pytest
from conftest import BUCKET, LLM_ID, REGION, SEC_ID, get, keys, put

from px_runner import tasks

ENV = {k: os.environ.get(k, "") for k in ("PX_TEST_PIPELINE_PYTHON", "PX_TEST_FORMD_DB", "PX_TEST_DOL_DIR",
                                          "PX_TEST_NPORT_CSV", "PX_TEST_EXPECT_WORK")}
pytestmark = pytest.mark.skipif(not all(ENV[k] for k in ("PX_TEST_PIPELINE_PYTHON", "PX_TEST_FORMD_DB", "PX_TEST_DOL_DIR")),
                                reason="real run is opt-in: set PX_TEST_PIPELINE_PYTHON, PX_TEST_FORMD_DB, PX_TEST_DOL_DIR")
CHECKOUT = Path(__file__).resolve().parents[3]
CO, RUN = "lungpacer", "20261117T120000Z-realrun1"
DOL = ("form5500_rows.csv", "form5500sf_rows.csv", "schedule_h_rows.csv")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_aliases_and_records_for_lungpacer(aws, tmp_path):
    s3, sm = aws
    originals = [Path(ENV["PX_TEST_FORMD_DB"]), *(Path(ENV["PX_TEST_DOL_DIR"]) / n for n in DOL),
                 *([Path(ENV["PX_TEST_NPORT_CSV"])] if ENV["PX_TEST_NPORT_CSV"] else []), CHECKOUT / "pipeline/config/lungpacer.json"]
    before = {p: (sha(p), p.stat().st_mtime_ns) for p in originals}

    repo = tmp_path / "repo"                                            # the pipeline code, without company-finder/.env
    for part in ("pipeline", "tools"):
        shutil.copytree(CHECKOUT / part, repo / part, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".env", ".env.*"))
    seed = tmp_path / "seed"
    seed.mkdir()
    cfg = json.loads((CHECKOUT / "pipeline/config/lungpacer.json").read_text(encoding="utf-8"))
    put(s3, f"input/{CO}/company.json", json.dumps({**cfg, "run_indirect": False}, indent=1))
    for src, key in [(Path(ENV["PX_TEST_FORMD_DB"]), "reference/formd/formd.duckdb"),
                     *((Path(ENV["PX_TEST_DOL_DIR"]) / n, f"reference/dol/raw/{n}") for n in DOL),
                     *([(Path(ENV["PX_TEST_NPORT_CSV"]), f"reference/nport/{CO}/nport_holdings.csv")] if ENV["PX_TEST_NPORT_CSV"] else [])]:
        copy = seed / key.replace("/", "__")
        shutil.copy2(src, copy)
        s3.upload_file(str(copy), BUCKET, key)

    s = tasks.Settings(bucket=BUCKET, region=REGION, secret_sec=SEC_ID, secret_llm=LLM_ID, local_root=tmp_path / "px",
                       repo=repo, python=ENV["PX_TEST_PIPELINE_PYTHON"], workers=2, nport_db="")
    for task in ("aliases", "records"):
        assert tasks.run(task, CO, RUN, s, s3, sm) == 0, json.loads(get(s3, f"runs/{CO}/{RUN}.json"))["tasks"][task]
    rec = json.loads(get(s3, f"runs/{CO}/{RUN}.json"))
    print("\nrun record:", json.dumps(rec, indent=1))
    assert sm.ids == []                                                 # neither task reads a secret
    assert rec["status"] == "running" and {t: rec["tasks"][t]["status"] for t in rec["tasks"]} == {
        "aliases": "succeeded", "records": "succeeded"}

    # aliases: the companies file make_companies_file.py builds from the config
    i = f"intermediate/{CO}/{RUN}"
    assert keys(s3, f"{i}/mentions/") == {f"{i}/mentions/input/lungpacer_input.csv"}
    rows = list(csv.DictReader(io.StringIO(get(s3, f"{i}/mentions/input/lungpacer_input.csv").decode("utf-8"))))
    canonical = cfg["company"].get("legal_name") or cfg["name"]
    aliases = [a for a in dict.fromkeys([cfg["name"], *cfg["company"]["aliases"]]) if a.lower() != canonical.lower()]
    assert rows == [{"id": "C" + cfg["company"]["cik"].zfill(10), "canonical_name": canonical, "aliases": "|".join(aliases),
                     "description": cfg["company"].get("description", "")}]

    # records: Form D built from the database, DOL 401(k) rows written; welfare_plans_by_year.csv is not produced
    # today, so records.py reports the DOL step incomplete after writing them (ARCHITECTURE section 8)
    out = rec["tasks"]["records"]["outputs"]
    nport = bool(ENV["PX_TEST_NPORT_CSV"])
    assert out["nport"] == ("reference" if nport else "none")
    assert out["not_built"] == ([] if nport else ["N-PORT price marks"]) + ["DOL Form 5500"]   # records.py's order
    expected = {"formd_offerings.csv", "inputs/data/dol_form5500.csv", "inputs/data/plan_participants.csv",
                *(["inputs/data/nport_price_monthly.csv", "nport/nport_holdings.csv"] if nport else [])}
    assert {k.removeprefix(f"{i}/records/") for k in keys(s3, f"{i}/records/")} == expected
    assert out["rows"]["formd_offerings.csv"] > 0

    if ENV["PX_TEST_EXPECT_WORK"]:                                      # same bytes as the earlier local run
        work = Path(ENV["PX_TEST_EXPECT_WORK"])
        local = {"formd_offerings.csv": work / "out" / CO / "formd_offerings.csv",
                 **{f"inputs/data/{p.name}": p for p in (work / "inputs" / CO / "data").glob("*.csv")}}
        compared = {k: get(s3, f"{i}/records/{k}") == p.read_bytes() for k, p in local.items() if k in expected}
        print("compared with", work, compared)
        assert compared and all(compared.values()), compared

    assert {p: (sha(p), p.stat().st_mtime_ns) for p in originals} == before  # data and checkout config untouched
