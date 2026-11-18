#!/usr/bin/env python3
"""The expected workbook for the end-to-end comparison: the main branch's pipeline code, run on this machine (no
container, no S3, no model call) from the same filings and reference data as the end-to-end run, with the language-model
outputs of an earlier run (company-finder's kept and dropped mentions, the indirect step's answers).

    python deploy/e2e/build_expected.py --out DIR                       # model outputs of the local run in work/
    python deploy/e2e/build_expected.py --out DIR --from-run RUN_ID     # model outputs of an end-to-end run (moto)

--from-run reads the moto server of that run, which run_e2e.py keeps only with --keep-moto (stop it afterwards with
run_e2e.py --steps stop).

DIR/repo is `git archive <ref> pipeline tools company-finder` (the committed code, nothing else); DIR/data a copy of the
filings and reference data (run_e2e.py's sources); DIR/finder and DIR/work/out/<co>/indirect the model outputs. Then,
with the pipeline's own Python and the PX_* folders pointing into DIR:

    pipeline/run_company.py lungpacer --cik ... --name "<legal name>" --dest DIR/books --workers N   (no --indirect)
    pipeline/indirect/locate_mentions.py lungpacer DIR/work/out/lungpacer/indirect
    pipeline/findings_excel.py lungpacer "<legal name>" DIR/books

-> DIR/books/<Name>_findings.xlsx. No secret is in the environment of these scripts, and DIR/repo has no .env, so a model
call cannot happen (it would fail). The checkout and the original data are only read.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

import run_e2e as e2e

CO = e2e.CO


def copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=False, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)


def from_s3(port: int, host: str, prefix: str, dest: Path) -> int:
    """Every object under prefix to dest/<rest of the key>, checked as s3sync.Bucket.download_dir checks it: nothing
    is written outside dest (moto has no authentication, so any local process could have written the keys)."""
    s3 = e2e.aws("s3", port, host)
    jobs = []
    for key in e2e.listing(s3, prefix):
        rel = PurePosixPath(key[len(prefix):])
        if key.endswith("/") or not rel.parts:                     # a folder marker
            continue
        if rel.is_absolute() or ".." in rel.parts:
            sys.exit(f"unsafe S3 key under {prefix}: {key}")
        jobs.append((key, dest.joinpath(*rel.parts)))
    for key, out in jobs:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(e2e.get_bytes(s3, key))
    return len(jobs)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True, help="new or empty scratch folder")
    ap.add_argument("--ref", default="main", help="git ref of the pipeline code (default: main)")
    ap.add_argument("--python", type=Path, default=e2e.REPO / "company-finder" / ".venv" / "bin" / "python",
                    help="the Python that runs the pipeline (default: company-finder/.venv, the local environment)")
    ap.add_argument("--workers", default="2", help="run_company.py --workers (its default, 2)")
    ap.add_argument("--finder", type=Path, default=e2e.REPO / "work" / "finder",
                    help=f"company-finder root with input/{CO}_input.csv and out/{CO}/ (default: the checkout's work/finder)")
    ap.add_argument("--indirect", type=Path, default=e2e.REPO / "work" / "out" / CO / "indirect",
                    help="the indirect step's output folder (default: the checkout's work/out/<co>/indirect)")
    ap.add_argument("--from-run", help="take the finder and indirect outputs of this end-to-end run from the moto server")
    ap.add_argument("--port", type=int, default=5055, help="port of the moto server of that run (run_e2e.py --keep-moto)")
    ap.add_argument("--moto-host", default="127.0.0.1", help="address of that moto server")
    for k, v in e2e.SOURCES.items():
        if k != "env":
            ap.add_argument(f"--{k}", type=Path, default=v, help=f"source: default {v}")
    a = ap.parse_args()
    out = a.out.resolve()
    if out.exists() and any(out.iterdir()):
        sys.exit(f"{out} is not empty")
    repo, data, finder, work, books = out / "repo", out / "data", out / "finder", out / "work", out / "books"
    repo.mkdir(parents=True)

    # the committed pipeline code of --ref
    archive = subprocess.run(["git", "-C", str(e2e.REPO), "archive", a.ref, "pipeline", "tools", "company-finder"],
                             capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(repo)], input=archive, check=True)
    commit = subprocess.run(["git", "-C", str(e2e.REPO), "rev-parse", a.ref], capture_output=True, text=True,
                            check=True).stdout.strip()
    print(f"code: git archive {a.ref} ({commit[:12]}) -> {repo}")

    # the same filings and reference data as the end-to-end run (copies)
    dumps = data / "dumps"
    copy(a.files, dumps / CO)
    copy(a.manifest, dumps / f"{CO}_manifest.csv")
    copy(a.nport, dumps / f"{CO}_nport" / "nport_holdings.csv")
    for n in e2e.DOL_FILES:
        copy(a.dol / n, dumps / "employee_docs" / "raw" / n)
    copy(a.formd, data / "input" / "formd" / "formd.duckdb")
    print(f"data: copies of the filings, N-PORT, DOL and Form D files -> {data}")

    # the language-model outputs
    ind = work / "out" / CO / "indirect"
    if a.from_run:
        base = f"intermediate/{CO}/{a.from_run}/"
        n1 = from_s3(a.port, a.moto_host, base + "mentions/", finder)
        n2 = from_s3(a.port, a.moto_host, base + "indirect/", ind)
        print(f"model outputs: run {a.from_run} from the moto server: {n1} finder files, {n2} indirect files")
    else:
        copy(a.finder / "input" / f"{CO}_input.csv", finder / "input" / f"{CO}_input.csv")
        copy(a.finder / "out" / CO, finder / "out" / CO)
        copy(a.indirect, ind)
        print(f"model outputs: {a.finder} (finder), {a.indirect} (indirect)")
    if not (finder / "out" / CO / f"{CO}_mentions.csv").is_file() or not any(ind.glob("*_indirect_run.json")):
        sys.exit("the finder mentions or the indirect answers are missing")

    cfg = json.loads((repo / "pipeline" / "config" / f"{CO}.json").read_text(encoding="utf-8"))
    cik = re.sub(r"\D", "", str(cfg["company"]["cik"])).zfill(10)
    with open(finder / "input" / f"{CO}_input.csv", newline="", encoding="utf-8-sig") as fh:
        name = next(csv.DictReader(fh))["canonical_name"]                 # the legal name the finder ran with
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update(PX_DATA=str(data), PX_DUMPS=str(dumps), PX_FINDER_ROOT=str(finder), PX_WORK=str(work),
               PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
    py = str(a.python)
    steps = [["pipeline/run_company.py", CO, "--cik", cik, "--name", name, "--dest", str(books), "--workers", a.workers,
              "--formd-db", str(data / "input" / "formd" / "formd.duckdb")],
             ["pipeline/indirect/locate_mentions.py", CO, str(ind)],
             ["pipeline/findings_excel.py", CO, name, str(books)]]
    log = open(out / "build.log", "w", encoding="utf-8")
    for step in steps:
        print("$ " + " ".join([py, *step]), flush=True)
        log.write("$ " + " ".join([py, *step]) + "\n")
        log.flush()
        r = subprocess.run([py, *step], cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT)
        if r.returncode:
            sys.exit(f"{step[0]} failed (exit {r.returncode}); see {out / 'build.log'}")
    book = sorted(books.glob("*.xlsx"))
    print(f"expected workbook: {book[0] if book else 'NONE'} (log: {out / 'build.log'})")
    return 0 if book else 1


if __name__ == "__main__":
    sys.exit(main())
