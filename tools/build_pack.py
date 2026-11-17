#!/usr/bin/env python3
"""One command from a folder of downloaded filings to the company's pack: <Name>_findings.xlsx and <Name>_report.docx.

    python tools/build_pack.py kalshi --cik 0001806928 --name "Kalshi Inc." \
        --filings <dir with <FORM>/ subfolders> --manifest <kalshi_manifest.csv> \
        --nport-db nport_team.duckdb --formd-db formd.duckdb --employee-dir employee_docs/raw \
        --template Databricks_report.docx --dest <output folder> [--finder-input kalshi_input.csv] [--hand-checked 3]

Steps (each one is an existing script; this file only runs them in order and passes the folders):
  1. filings        links --filings and --manifest into $PX_DUMPS/<co>/ and $PX_DUMPS/<co>_manifest.csv
  2. string match   company-finder: exact names and aliases, one row per company per paragraph (--dedupe-passages),
                    look-alike names dropped by the model (--llm-disambiguate)  -> $PX_FINDER_ROOT/out/<co>/<co>_mentions.csv
                    companies file: --finder-input, else tools/make_companies_file.py from pipeline/config/<co>.json
  3. N-PORT         sources/nport.py export-db (team database) + build       -> $PX_DUMPS/<co>_nport/nport_holdings.csv
  4. records        sources/records.py: N-PORT price marks, DOL Form 5500, Form D offerings
  5. pipeline       pipeline/run_company.py --skip-records [--indirect]: evidence, prepare, indirect mentions, workbook
  6. pack           the workbook copied to --dest as <Name>_findings.xlsx; tools/report_docx.py -> <Name>_report.docx

Folders: PX_DUMPS and PX_FINDER_ROOT default to <PX_WORK>/dumps and <PX_WORK>/finder here (set them to use others), so
the source folders are only read. The indirect step and --llm-disambiguate call models (OPENROUTER_API_KEY in
company-finder/.env); --no-indirect and --no-llm skip them.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORK = Path(os.environ.get("PX_WORK", REPO / "work"))
os.environ.setdefault("PX_DUMPS", str(WORK / "dumps"))          # before paths.py is read, here and in every step
os.environ.setdefault("PX_FINDER_ROOT", str(WORK / "finder"))
sys.path.insert(0, str(REPO / "pipeline"))
import paths as px  # noqa: E402

PY = sys.executable


def run(args: list, cwd: Path = REPO, log: Path | None = None) -> None:
    print(f"== {' '.join(str(x) for x in args)}", flush=True)
    if log:
        with open(log, "w", encoding="utf-8") as fh:
            p = subprocess.Popen([str(x) for x in args], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in p.stdout:                       # type: ignore[union-attr]
                fh.write(line)
                print(line, end="", flush=True)
            code = p.wait()
    else:
        code = subprocess.run([str(x) for x in args], cwd=cwd).returncode
    if code:
        sys.exit(f"step failed (exit {code}): {args[1] if len(args) > 1 else args[0]}")


def link(src: Path, dest: Path) -> None:
    src = src.resolve()
    if not src.exists():
        sys.exit(f"not found: {src}")
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    elif dest.exists():
        sys.exit(f"{dest} exists and is a real folder; remove it or set PX_DUMPS")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(src, target_is_directory=src.is_dir())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("company", help="config name: pipeline/config/<company>.json")
    ap.add_argument("--cik", required=True, help="the company's own CIK (Form D)")
    ap.add_argument("--name", required=True, help='legal name for the workbook and report, e.g. "Kalshi Inc."')
    ap.add_argument("--filings", required=True, help="downloaded filings, one subfolder per form (tools/download_filings.py layout)")
    ap.add_argument("--manifest", help="the filings' manifest CSV (default: <filings>/../<company>_manifest.csv)")
    ap.add_argument("--finder-input", help="company-finder companies file; default: built from the config")
    ap.add_argument("--nport-db", help="N-PORT team database (nport_team.duckdb); without it N-PORT is skipped")
    ap.add_argument("--formd-db", default=str(px.FORMD_DB))
    ap.add_argument("--employee-dir", default=str(px.DUMPS / "employee_docs" / "raw"), help="DOL Form 5500 rows (form5500_rows.csv ...)")
    ap.add_argument("--template", required=True, help="Word template (the Databricks report)")
    ap.add_argument("--dest", required=True, help="folder for <Name>_findings.xlsx and <Name>_report.docx")
    ap.add_argument("--hand-checked", type=int, help="indirect words given to the company that were read by hand and found correct")
    ap.add_argument("--no-llm", action="store_true", help="string match without the look-alike check")
    ap.add_argument("--no-indirect", action="store_true", help="skip the indirect step (no model cost)")
    ap.add_argument("--skip-finder", action="store_true", help="reuse the company-finder output already in PX_FINDER_ROOT")
    ap.add_argument("--workers", default="2")
    a = ap.parse_args()
    co = a.company
    cfg_path = px.config_path(co)
    if not cfg_path.exists():
        sys.exit(f"no config: {cfg_path} (copy pipeline/config/kalshi.json)")
    short = (json.loads(cfg_path.read_text(encoding="utf-8")).get("name") or a.name.split(",")[0]).replace(" ", "_")
    filings = Path(a.filings)
    manifest = Path(a.manifest) if a.manifest else filings.parent / f"{co}_manifest.csv"
    print(f"{co}: PX_DUMPS={px.DUMPS}  PX_FINDER_ROOT={px.FINDER_ROOT}  PX_WORK={px.WORK}", flush=True)

    # 1. filings (read in place, through links)
    link(filings, px.DUMPS / co)
    link(manifest, px.DUMPS / f"{co}_manifest.csv")

    # 2. string match + paragraph de-duplication + look-alike check
    out = px.FINDER_ROOT / "out" / co
    if not a.skip_finder:
        inp = px.FINDER_ROOT / "input" / f"{co}_input.csv"
        inp.parent.mkdir(parents=True, exist_ok=True)
        if a.finder_input:
            shutil.copyfile(a.finder_input, inp)
        else:
            run([PY, REPO / "tools" / "make_companies_file.py", co, "--name", a.name, "--out", inp])
        out.mkdir(parents=True, exist_ok=True)
        run([PY, "company_finder.py", "-c", inp, "-i", px.DUMPS / co, "-f", "csv", "-o", out / f"{co}.csv", "--dedupe-passages",
             *([] if a.no_llm else ["--llm-disambiguate"])], cwd=px.FINDER, log=out / "run.log")

    # 3. N-PORT from the team database
    nport = px.DUMPS / f"{co}_nport"
    nport.mkdir(parents=True, exist_ok=True)
    if a.nport_db:
        run([PY, REPO / "pipeline" / "sources" / "nport.py", "export-db", "--dump", nport, "--db", a.nport_db])
        run([PY, REPO / "pipeline" / "sources" / "nport.py", "build", "--dump", nport, "--out", nport / "nport_holdings.csv"])
    else:
        print("N-PORT: no --nport-db, skipped", flush=True)

    # 4. structured records, 5. evidence -> prepare -> indirect -> workbook
    run([PY, REPO / "pipeline" / "sources" / "records.py", co, "--cik", a.cik, "--formd-db", a.formd_db, "--employee-dir", a.employee_dir])
    staging = px.WORK / "deliver" / co
    run([PY, REPO / "pipeline" / "run_company.py", co, "--cik", a.cik, "--name", a.name, "--dest", staging, "--formd-db", a.formd_db,
         "--skip-records", "--workers", a.workers, *([] if a.no_indirect else ["--indirect"])])

    # 6. the pack
    dest = Path(a.dest)
    dest.mkdir(parents=True, exist_ok=True)
    made = staging / f"{a.name.split(',')[0].replace(' ', '_')}_findings.xlsx"
    xlsx, docx_out = dest / f"{short}_findings.xlsx", dest / f"{short}_report.docx"
    shutil.copyfile(made, xlsx)
    run([PY, REPO / "tools" / "report_docx.py", co, a.name, xlsx, docx_out, "--template", a.template,
         *([] if a.hand_checked is None else ["--hand-checked", str(a.hand_checked)])])
    print(f"done: {xlsx}\n      {docx_out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
