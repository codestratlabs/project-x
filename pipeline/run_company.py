#!/usr/bin/env python3
"""One command for a company, after its filings are downloaded and string-matched (see README): structured records,
the rule-based evidence, the indirect mentions (language model, only with --indirect) and the findings workbook.
The same script runs for every company; everything company-specific is in pipeline/config/<company>.json.

    python pipeline/run_company.py databricks --cik 0001587468 --name "Databricks, Inc." --dest <folder for the workbook>
        [--indirect] [--skip-records] [--skip-scan] [--skip-prepare]

Steps
  1. records         sources/records.py: N-PORT price marks, DOL Form 5500 rows, Form D offerings
  2. evidence        evidence/evidence_tables -> evidence_build -> evidence_holdings -> validate_all
                     -> evidence_holdings --use-validation -> evidence_facts -> locate      (no model)
  3. prepare         prepare_filings.py for each dump folder: each filing with its company-finder mentions
  4. indirect        indirect/run_all.py (GPT-4.1 + a second model; costs money, so only with --indirect)
                     -> indirect/locate_mentions.py (page and highlight link of each mention)
  5. workbook        findings_excel.py -> <dest>/<Name>_findings.xlsx
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import paths as px  # noqa: E402


def run(script: str, *args: str) -> None:
    print(f"== {script} {' '.join(args)}", flush=True)
    if subprocess.run([sys.executable, str(HERE / script), *args]).returncode:
        sys.exit(f"step failed: {script}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("company", help="the config name: pipeline/config/<company>.json and the dump folder prefix")
    ap.add_argument("--cik", required=True, help="the company's own CIK (Form D)")
    ap.add_argument("--name", required=True, help='the company name as it should appear in the workbook, e.g. "Databricks, Inc."')
    ap.add_argument("--dest", required=True, help="folder for the findings workbook")
    ap.add_argument("--formd-db", default=str(px.FORMD_DB))
    ap.add_argument("--indirect", action="store_true", help="run the indirect step (language model, costs money)")
    ap.add_argument("--skip-records", action="store_true", help="reuse the structured records already built")
    ap.add_argument("--skip-scan", action="store_true", help="reuse parsed_rows.csv instead of reading every filing again")
    ap.add_argument("--skip-prepare", action="store_true", help="reuse the prepared filings")
    ap.add_argument("--workers", default="2")
    a = ap.parse_args()
    co = a.company
    if not px.config_path(co).exists():
        sys.exit(f"no config: {px.config_path(co)}")
    folders = [f for f in (co, f"{co}_ncsr", f"{co}_delta") if (px.DUMPS / f"{f}_manifest.csv").exists()]
    if not folders:
        sys.exit(f"no downloaded filings: {px.DUMPS / (co + '_manifest.csv')}")
    print(f"{co}: dump folders {', '.join(folders)}; work folder {px.WORK}", flush=True)

    if not a.skip_records:
        run("sources/records.py", co, "--cik", a.cik, "--formd-db", a.formd_db)
    if not a.skip_scan:
        run("evidence/evidence_tables.py", co, "--folders", *folders, "--workers", a.workers)
    for step in (["evidence/evidence_build.py", co], ["evidence/evidence_holdings.py", co], ["evidence/validate_all.py", co],
                 ["evidence/evidence_holdings.py", co, "--use-validation"], ["evidence/evidence_facts.py", co, a.name], ["evidence/locate.py", co]):
        run(*step)
    if not a.skip_prepare:
        for i, f in enumerate(folders):
            run("prepare_filings.py", f, *([] if i == 0 else ["--link"]))
    if a.indirect:
        ind = px.OUT / co / "indirect"
        run("indirect/run_all.py", co, str(ind), "--parallel", a.workers)
        run("indirect/locate_mentions.py", co, str(ind))
    run("findings_excel.py", co, a.name, a.dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
