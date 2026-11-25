"""Settings and the tasks of deploy/INTERFACES.md: each stages its inputs from S3, runs the pipeline script(s) and
uploads its outputs; every task is recorded in the run record.

A task runs in a fresh local root (PX_LOCAL_ROOT, wiped first) that the pipeline sees through PX_DATA, PX_DUMPS,
PX_FINDER_ROOT and PX_WORK. Commands run with PX_PYTHON in PX_REPO and their output is streamed to stdout. Only
download (SEC_USER_AGENT), finder and indirect (OPENROUTER_API_KEY) read a secret, and only for their subprocess.
"""
from __future__ import annotations

import ast
import collections
import csv
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import traceback
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from . import runrecord, s3sync, secrets

TASKS = (*runrecord.PIPELINE, "finalize")
TASK_SECRET = {"download": secrets.SEC_USER_AGENT, "finder": secrets.OPENROUTER_API_KEY,
               "indirect": secrets.OPENROUTER_API_KEY}
TAIL = 40                                      # output lines kept for the run record when a script fails
FORMD = "Form D offerings"                     # the source name records.py prints (not optional, unlike DOL and N-PORT)
# One filing's results in the indirect folder: <document stem><suffix>. indirect_mentions.py writes the two CSV files,
# then the run file; the runner then adds the input file (the SHA-256 of the prepared files the answers were made from).
RUN_JSON, INPUT_JSON = "_indirect_run.json", "_indirect_input.json"
INDIRECT_FILES = (RUN_JSON, "_indirect_mentions.csv", "_indirect_all_triggers.csv", INPUT_JSON)
COSTS_JSON = "model_cost.json"                 # in the indirect folder: the cost of every run file of this run (note_costs)
csv.field_size_limit(2**31 - 1)                # finder mention rows carry long context fields


def log(msg: str) -> None:
    print(f"[px_runner] {msg}", flush=True)


def find_repo() -> Path:
    """The checkout this package sits in (the folder with pipeline/paths.py), else the image's /opt/project-x."""
    for folder in Path(__file__).resolve().parents:
        if (folder / "pipeline" / "paths.py").is_file():
            return folder
    return Path("/opt/project-x")


@dataclass(frozen=True)
class Settings:
    bucket: str
    region: str
    secret_sec: str
    secret_llm: str
    local_root: Path
    repo: Path
    python: str
    workers: int
    nport_db: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env

        def get(name: str, default: str) -> str:
            return (env.get(name) or "").strip() or default

        return cls(bucket=get("PX_BUCKET", "company-reports-data"), region=get("AWS_REGION", "us-east-2"),
                   secret_sec=get("PX_SECRET_SEC", "px/sec-user-agent"),
                   secret_llm=get("PX_SECRET_LLM", "px/openrouter-api-key"),
                   local_root=Path(get("PX_LOCAL_ROOT", "/tmp/px")), repo=Path(get("PX_REPO", "") or find_repo()),
                   python=get("PX_PYTHON", sys.executable), workers=int(get("PX_WORKERS", "4")),
                   nport_db=get("PX_NPORT_DB", ""))

    def secret_id(self, name: str) -> str:
        return {secrets.SEC_USER_AGENT: self.secret_sec, secrets.OPENROUTER_API_KEY: self.secret_llm}[name]


def client(service: str, settings: Settings):
    """A boto3 client (AWS_ENDPOINT_URL, the standard variable, points it at moto in tests)."""
    import boto3
    from botocore.config import Config
    return boto3.client(service, region_name=settings.region,
                        config=Config(retries={"mode": "standard", "max_attempts": 10}))


class TaskError(Exception):
    """A failure the runner found; outputs (counts, model cost) are still recorded."""

    def __init__(self, message: str, outputs: dict | None = None):
        super().__init__(message)
        self.outputs = outputs or {}


class ScriptFailed(TaskError):
    def __init__(self, script: str, code: int, tail: list[str]):
        super().__init__(f"{script} exited with code {code}\n" + "\n".join(tail))


class Terminated(Exception):
    """SIGTERM (Airflow timeout, container stop): recorded as a failure before the process exits."""


EXPECTED = (TaskError, secrets.SecretError, Terminated, s3sync.DeleteFailed)  # failures that need no traceback in the log


def wipe_local_root(local: Path, repo: Path) -> None:
    """Delete PX_LOCAL_ROOT and create it again, empty and private (mode 0700). Refused for a folder that is not
    scratch (/, the home folder, the repo or a folder above it), for a symbolic link (on a shared host another user
    could point /tmp/px at any folder this user may delete) and for a folder of another user. The path itself is
    deleted, never what a link points to."""
    root, repo, home = local.resolve(), repo.resolve(), Path(os.path.expanduser("~")).resolve()
    if root == Path(root.anchor) or root == home or root == repo or root in repo.parents:
        raise TaskError(f"PX_LOCAL_ROOT={root} is not a scratch folder; refusing to wipe it")
    if local.is_symlink():
        raise TaskError(f"PX_LOCAL_ROOT={local} is a symbolic link; refusing to wipe or use it")
    if local.exists():
        st = local.lstat()
        if st.st_uid != os.getuid():
            raise TaskError(f"PX_LOCAL_ROOT={local} belongs to another user (uid {st.st_uid}); refusing to wipe it")
        if not stat.S_ISDIR(st.st_mode):
            raise TaskError(f"PX_LOCAL_ROOT={local} is not a folder; refusing to wipe it")
        shutil.rmtree(local)
    try:
        local.mkdir(mode=0o700, parents=True)
    except FileExistsError:                      # created by someone else between the wipe and here
        raise TaskError(f"PX_LOCAL_ROOT={local} appeared again while it was wiped; refusing to use it") from None


def pipeline_env(base: Mapping[str, str], lay: s3sync.Layout) -> dict[str, str]:
    """The pipeline subprocess environment: no secret and no AWS setting is inherited (Job.use_secret adds the task's
    own secret), and the PX_* folders point into the local root."""
    env = {k: v for k, v in base.items() if k not in secrets.NAMES and not k.startswith("AWS_")}
    env.update(PX_DATA=str(lay.data), PX_DUMPS=str(lay.dumps), PX_FINDER_ROOT=str(lay.finder), PX_WORK=str(lay.work),
               PYTHONUNBUFFERED="1")
    return env


class Job:
    """One task of one run: settings, S3, the key table, the company config and the pipeline environment."""

    def __init__(self, task: str, co: str, run_id: str, settings: Settings, bucket: s3sync.Bucket, sm):
        self.task, self.co, self.run_id, self.s, self.bucket, self.sm = task, co, run_id, settings, bucket, sm
        self.lay = s3sync.Layout(settings.local_root, settings.repo, co, run_id)
        self.cfg: dict = {}
        self.env: dict[str, str] = {}
        self.secret_values: list[str] = []
        self.record: dict = {}                   # the run record as this task found it (its own entry set to running)

    # ------------------------------------------------------------------ setup
    def prepare(self, planned_etag: str | None) -> None:
        """Wipe the local root; write input/<co>/company.json to <PX_REPO>/pipeline/config/<co>.json."""
        wipe_local_root(self.s.local_root, self.s.repo)
        key = self.lay.config.key
        got = self.bucket.read(key)
        if got is None:
            raise TaskError(f"missing s3://{self.bucket.name}/{key}")
        data, etag = got
        try:
            cfg = json.loads(data)
        except ValueError as e:
            raise TaskError(f"{key} is not valid JSON: {e}") from None
        if not isinstance(cfg, dict) or not isinstance(cfg.get("company", {}), dict):
            raise TaskError(f"{key}: expected a JSON object with a \"company\" object")
        if not isinstance(cfg.get("run_indirect", True), bool):
            raise TaskError(f"{key}: run_indirect must be true or false")
        if planned_etag and etag != planned_etag.strip('"'):
            log(f"WARNING {key} changed since this run was planned (ETag {etag}, planned {planned_etag}); "
                "using the current file")
        self.lay.config.path.parent.mkdir(parents=True, exist_ok=True)
        self.lay.config.path.write_bytes(data)
        self.cfg = cfg
        self.env = pipeline_env(os.environ, self.lay)

    def use_secret(self, name: str | None) -> None:
        if name:
            value = secrets.fetch(self.sm, self.s.secret_id(name), name)
            self.env[name] = value
            self.secret_values.append(value)

    @property
    def company_name(self) -> str:
        """<Name> for the workbook: company.legal_name, else name."""
        name = str(self.cfg.get("company", {}).get("legal_name") or self.cfg.get("name") or "").strip()
        if not name:
            raise TaskError(f"{self.lay.config.key} has neither company.legal_name nor name")
        return name

    def redact(self, text: str) -> str:
        return secrets.redact(text, self.secret_values)

    # ------------------------------------------------------------------ scripts
    def run(self, script: str, *args) -> list[str]:
        """Run a pipeline script (path relative to PX_REPO) and stream its output; returns the last lines."""
        cmd = [self.s.python, script, *map(str, args)]
        log("$ " + self.redact(" ".join(cmd)))
        proc = subprocess.Popen(cmd, cwd=self.s.repo, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                encoding="utf-8", errors="replace")
        tail: collections.deque[str] = collections.deque(maxlen=TAIL)
        try:
            for line in proc.stdout:
                line = self.redact(line.rstrip("\n"))
                print(line, flush=True)
                tail.append(line)
            code = proc.wait()
        except BaseException:                    # Terminated, or an error while reading: do not leave the script running
            proc.kill()
            proc.wait()
            raise
        if code:
            raise ScriptFailed(script, code, list(tail))
        return list(tail)

    # ------------------------------------------------------------------ S3 <-> local
    def stage(self, loc: s3sync.Loc, required: bool = True) -> bool:
        if self.bucket.download(loc.key, loc.path):
            return True
        if required:
            raise TaskError(f"missing s3://{self.bucket.name}/{loc.key}")
        return False

    def stage_dir(self, loc: s3sync.Loc, sub: str = "") -> int:
        return self.bucket.download_dir(loc.key + sub, loc.path / sub if sub else loc.path)

    def stage_raw(self, required: bool = True) -> int:
        self.stage(self.lay.manifest, required)
        return self.stage_dir(self.lay.raw)

    def stage_records(self) -> None:
        self.stage_dir(self.lay.records_inputs)
        self.stage(self.lay.formd_offerings, required=False)
        self.stage_dir(self.lay.records_nport)

    def upload(self, loc: s3sync.Loc, required: bool = True) -> list[tuple[str, Path]]:
        if loc.path.is_file():
            self.bucket.upload(loc.path, loc.key)
            return [(loc.key, loc.path)]
        if required:
            raise TaskError(f"{loc.path} was not written")
        return []

    def upload_dir(self, loc: s3sync.Loc, unchanged: dict[str, int] | None = None) -> list[tuple[str, Path]]:
        return self.bucket.upload_dir(loc.path, loc.key, unchanged)

    def sync_dir(self, loc: s3sync.Loc, last: tuple[str, ...] = ()) -> list[tuple[str, Path]]:
        """A task's output folder under intermediate/: uploaded (the files ending in a suffix of last after the others),
        and what an earlier attempt left there removed."""
        return self.bucket.sync_dir(loc.path, loc.key, last)

    def sync_file(self, loc: s3sync.Loc) -> list[tuple[str, Path]]:
        return self.bucket.sync_file(loc.path, loc.key)


# ---------------------------------------------------------------------- the tasks
def aliases(job: Job) -> dict:
    job.run("tools/make_companies_file.py", job.co, "--config", job.lay.config.path, "--force")
    return {"files_uploaded": len(job.upload_dir(job.lay.mentions))}


def download(job: Job) -> dict:
    lay = job.lay
    before = job.bucket.sizes(lay.raw.key)
    job.stage_raw(required=False)                              # files on disk are kept: only new filings are fetched
    job.run("tools/download_filings.py", job.co, "--config", lay.config.path, "--dumps", lay.dumps)
    job.upload(lay.manifest)
    new = job.upload_dir(lay.raw, unchanged=before)            # new or changed (size) files; nothing in raw/ is deleted
    rows = read_csv(lay.manifest.path)
    return {"manifest_rows": len(rows), "failed_downloads": sum(r.get("status") != "ok" for r in rows),
            "new_or_changed_files": len(new)}


def finder(job: Job) -> dict:
    lay, co = job.lay, job.co
    job.stage_raw()
    job.stage_dir(lay.mentions, "input/")
    companies, out = lay.finder / "input" / f"{co}_input.csv", lay.finder / "out" / co / f"{co}.csv"
    if not companies.is_file():
        raise TaskError(f"missing s3://{job.bucket.name}/{lay.mentions.key}input/{co}_input.csv (written by aliases)")
    out.parent.mkdir(parents=True, exist_ok=True)              # company_finder.py does not create it
    job.run("company-finder/company_finder.py", "--companies", companies, "--input", lay.raw.path, "--out", out,
            "--format", "csv", "--llm-disambiguate")
    mentions = out.with_name(f"{co}_mentions.csv")             # one row per mention: what prepare_filings.py reads
    if not mentions.is_file():
        raise TaskError(f"company_finder.py wrote no {mentions.name}")
    return {"mentions": csv_rows(mentions), "dropped": csv_rows(out.with_name(f"{co}_dropped.csv")),
            "files_uploaded": len(job.sync_dir(lay.mentions))}


def records(job: Job) -> dict:
    lay, s = job.lay, job.s
    cik = str(job.cfg.get("company", {}).get("cik") or "").strip()
    if not cik:
        raise TaskError(f"{lay.config.key}: company.cik is not set")
    job.stage(lay.formd_db)
    for loc in lay.dol_rows:
        job.stage(loc)
    job.stage(lay.dol_welfare, required=False)                 # optional: records.py reports it missing
    nport = lay.records_nport.path
    if s.nport_db:
        job.run("pipeline/sources/nport.py", "export-db", "--dump", nport, "--db", s.nport_db, "--config", lay.config.path)
        job.run("pipeline/sources/nport.py", "build", "--dump", nport, "--out", nport / "nport_holdings.csv",
                "--config", lay.config.path)
        source = "database"
    else:
        source = "reference" if job.stage(lay.nport_ref, required=False) else "none"
    tail = job.run("pipeline/sources/records.py", job.co, "--cik", cik, "--formd-db", lay.formd_db.path,
                   "--employee-dir", lay.employee_dir)
    not_built = [m[1] for line in tail if (m := re.match(r"MISSING (.+?): ", line))]
    if FORMD not in not_built and not lay.formd_offerings.path.is_file():     # its MISSING line left the tail
        not_built.append(FORMD)
    if FORMD in not_built:
        # records.py reports every source it could not build and exits 0, but only the DOL welfare file and N-PORT are
        # optional: Form D is written (empty) even for a company without notices, so MISSING here is an error, such as
        # a Form D file this duckdb cannot open or a filings table without a column it selects
        raise TaskError("records.py could not build the Form D offerings:\n"
                        + ("\n".join(line for line in tail if line.startswith(f"MISSING {FORMD}: "))
                           or f"it wrote no {lay.formd_offerings.path.name}; see the task log"),
                        {"nport": source, "not_built": not_built})
    up = job.sync_dir(lay.records_inputs) + job.sync_file(lay.formd_offerings) + job.sync_dir(lay.records_nport)
    base = lay.records_inputs.key.removesuffix("inputs/")
    return {"nport": source, "not_built": not_built,
            "rows": {k.removeprefix(base): csv_rows(p) for k, p in up if k.endswith(".csv")}, "files_uploaded": len(up)}


def evidence(job: Job) -> dict:
    lay, co, ev = job.lay, job.co, "pipeline/evidence/"
    job.stage_raw()
    job.stage_dir(lay.mentions)
    job.stage_records()
    for script, *args in ([ev + "evidence_tables.py", co, "--folders", co, "--workers", job.s.workers],
                          [ev + "evidence_build.py", co],
                          [ev + "evidence_holdings.py", co],
                          [ev + "validate_all.py", co],
                          [ev + "evidence_holdings.py", co, "--use-validation"],
                          [ev + "evidence_facts.py", co, job.company_name],
                          [ev + "locate.py", co],
                          ["pipeline/prepare_filings.py", co]):  # no --link: copies, so another machine can read them
        job.run(script, *args)
    up = job.sync_dir(lay.evidence) + job.sync_dir(lay.prepared)        # no earlier attempt's filings stay prepared
    return {"evidence_rows": csv_rows(lay.evidence.path / "evidence_rows.csv"),
            "holdings": csv_rows(lay.evidence.path / "holdings_from_evidence.csv"),
            "prepared_filings": len(list((lay.prepared.path / "companies").glob("*.meta.json"))), "files_uploaded": len(up)}


def indirect(job: Job) -> dict:
    lay, folder = job.lay, job.lay.indirect.path
    job.stage_dir(lay.prepared)
    job.stage_dir(lay.indirect)          # on a retry, run_all.py skips the filings an earlier attempt finished
    inputs = prepared_inputs(lay.prepared.path, job.co)
    costs = note_costs(folder, read_json(folder / COSTS_JSON) or {})     # before keep_finished removes any answers
    redone = keep_finished(folder, inputs)
    kept = set(folder.glob("*" + RUN_JSON))
    failure, tail = None, []
    try:
        tail = job.run("pipeline/indirect/run_all.py", job.co, folder, "--parallel", job.s.workers)
    except (ScriptFailed, Terminated) as exc:   # Terminated: stopped (timeout, task cleared) while the model runs
        failure = exc
    costs = note_costs(folder, costs, kept)
    note_inputs(folder, inputs)
    # Uploaded also after a failure or a stop, so a retry does not pay for those filings again. A filing's CSV files go
    # up before its run file, the run file before its input file, and the deletes come last: an upload cut short (a
    # SIGKILL at the stop timeout) leaves the earlier attempt's run or input file beside new CSV files, and keep_finished
    # then runs the filing again instead of keeping answers of two attempts.
    up = job.sync_dir(lay.indirect, last=(RUN_JSON, INPUT_JSON))
    runs = {p.name.removesuffix(RUN_JSON): read_json(p) or {} for p in sorted(folder.glob("*" + RUN_JSON))}
    incomplete = sorted(stem for stem, r in runs.items() if failed_calls(r))
    out = {"filings": len(runs), "failed_filings": int(run_all_counts(tail).get("fail", 0)),
           "failed_calls": sum(map(failed_calls, runs.values())), "redone_filings": redone,
           # every answer of this run's task 6 counts once, also those a retry replaced (the ledger, note_costs)
           "model_cost_usd": round(sum(float(c.get("cost_usd") or 0) for c in costs.values()), 4),
           "files_uploaded": len(up)}
    if failure:
        failure.outputs = out
        raise failure
    if out["failed_filings"]:
        raise TaskError(f"run_all.py: {out['failed_filings']} filing(s) failed; see {lay.indirect.key}run.log", out)
    if incomplete:
        # indirect_mentions.py exits 0 when model calls fail (credit limit, 401, 5xx after its retries): the filing's
        # answers are missing, so the run must not publish; a retry runs these filings again (keep_finished)
        raise TaskError(f"{len(incomplete)} filing(s) have model calls that failed (meta.failed_calls), for example "
                        f"{lay.indirect.key}{incomplete[0]}{RUN_JSON}; a retry runs them again", out)
    return out


def publish(job: Job) -> dict:
    lay = job.lay
    job.stage_raw()
    job.stage_records()
    job.stage_dir(lay.evidence)
    job.stage_dir(lay.prepared)
    # task 6's results only when it ran and succeeded in this run: files an earlier attempt left in indirect/ must not
    # reach the workbook once run_indirect is switched off (task 6 then records skipped)
    ran_indirect = bool(job.cfg.get("run_indirect", True)
                        and (job.record.get("tasks", {}).get("indirect") or {}).get("status") == "succeeded")
    if ran_indirect:
        job.stage_dir(lay.indirect)
        job.run("pipeline/indirect/locate_mentions.py", job.co, lay.indirect.path)
    job.run("pipeline/findings_excel.py", job.co, job.company_name, lay.reports)
    books = sorted(lay.reports.glob("*.xlsx"))
    if not books:
        raise TaskError("findings_excel.py wrote no workbook")
    latest_before = set(job.bucket.sizes(lay.report_latest.key))
    up = []
    for book in books:
        up += job.upload(s3sync.Loc(lay.report_run.key + book.name, book))
        up += job.upload(s3sync.Loc(lay.report_latest.key + book.name, book))
    job.bucket.delete(sorted(latest_before - {k for k, _ in up}))       # latest/ holds only this run's workbook
    up += job.sync_file(lay.located)
    return {"workbook": lay.report_run.key + books[0].name, "sheets": sheet_rows(books[0]), "indirect": ran_indirect,
            "files_uploaded": len(up)}


STEPS = {"aliases": aliases, "download": download, "finder": finder, "records": records, "evidence": evidence,
         "indirect": indirect, "publish": publish}


# ---------------------------------------------------------------------- one task, recorded
def run(task: str, co: str, run_id: str, settings: Settings, s3, sm) -> int:
    """Run one task and record it. Returns the exit code: 0 succeeded or skipped; for finalize, 0 once the run's status
    is recorded, whether succeeded or failed (company_evidence fails the DAG run of a failed run itself), so a
    non-zero finalize always means that finalize itself did not work."""
    bucket = s3sync.Bucket(s3, settings.bucket)
    if task == "finalize":
        rec, bad = runrecord.finalize(bucket, co, run_id)
        log(f"run {co}/{run_id} {rec['status']}" + (f"; not succeeded: {', '.join(bad)}" if bad else ""))
        return 0
    job = Job(task, co, run_id, settings, bucket, sm)
    started, t0 = runrecord.now(), time.monotonic()
    rec = runrecord.start_task(bucket, co, run_id, task, started, job.lay.config.key)
    job.record = rec
    status, outputs, error = "succeeded", {}, None
    try:
        job.prepare(rec.get("input_etag"))
        if task == "indirect" and not job.cfg.get("run_indirect", True):
            status, outputs = "skipped", {"reason": "run_indirect is false"}
        else:
            job.use_secret(TASK_SECRET.get(task))
            outputs = STEPS[task](job)
    except Exception as exc:
        status, outputs = "failed", getattr(exc, "outputs", {})
        error = job.redact(str(exc) if isinstance(exc, TaskError) else f"{type(exc).__name__}: {exc}")
        if not isinstance(exc, EXPECTED):
            print(job.redact(traceback.format_exc()), flush=True)
    seconds = time.monotonic() - t0
    runrecord.finish_task(bucket, co, run_id, task, status, started, seconds, outputs, error)
    log(f"{task} {status} in {seconds:.1f}s: {job.redact(json.dumps(outputs))}"
        + (f"\n[px_runner] error: {error.splitlines()[0]}" if error else ""))
    return 1 if status == "failed" else 0


# ---------------------------------------------------------------------- small readers
def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        return list(csv.DictReader(fh))


def csv_rows(path: Path) -> int | None:
    """Data rows of a CSV file (None when it does not exist)."""
    if not path.is_file():
        return None
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        return max(sum(1 for _ in csv.reader(fh)) - 1, 0)


def sha256(path: Path) -> str:
    """The SHA-256 of a file's content, or "missing"."""
    if not path.is_file():
        return "missing"
    with open(path, "rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def prepared_inputs(prepared: Path, co: str) -> dict[str, dict[str, str]]:
    """The prepared filings (run_all.py's glob) by document stem, the name of each filing's indirect results: the
    SHA-256 of every prepared file its answers are made from, by path under prepared/. That is the .meta.json (the
    filer), the mentions file (which pronouns go to the model) and the filing itself."""
    out: dict[str, dict[str, str]] = {}
    for meta in sorted((prepared / "companies").glob(f"{co}*__*_mentions.meta.json")):
        document = json.loads(meta.read_text(encoding="utf-8"))["document"]
        files = (meta, meta.with_name(meta.name.removesuffix(".meta.json") + ".csv"), prepared / "pdfs" / document)
        out.setdefault(Path(document).stem, {}).update((f.relative_to(prepared).as_posix(), sha256(f)) for f in files)
    return out


def read_json(path: Path) -> dict | None:
    """A JSON object from a file (a filing's run or input file, the cost ledger), or None when it cannot be read."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def failed_calls(run: dict) -> int:
    """Model calls that returned nothing (HTTP error after retries). A cut-off answer with content is not one."""
    meta = run.get("meta")
    return int((meta.get("failed_calls") if isinstance(meta, dict) else 0) or 0)


def run_cost(run: dict) -> float:
    return sum(float(c.get("cost_usd") or 0) for c in run.get("calls") or [] if isinstance(c, dict))


def keep_finished(folder: Path, inputs: dict[str, dict[str, str]]) -> int:
    """Before run_all.py, which skips every filing that has a run file: remove the results it must not skip, so those
    filings run again. A prepared filing's results are kept only when its four files are all there (not an upload cut
    short), its run file can be read and has no failed model call, and its input file names the prepared files staged
    now (inputs, prepared_inputs): answers made from an earlier attempt's mentions (finder or evidence ran again) are
    not kept. The results of a filing that is no longer prepared are removed. Returns the filings that will run again."""
    stems = {p.name.removesuffix(suffix) for suffix in INDIRECT_FILES for p in folder.glob("*" + suffix)}
    redo = dropped = 0
    for stem in sorted(stems):
        files = [folder / (stem + suffix) for suffix in INDIRECT_FILES]
        run = read_json(files[0])
        if (stem in inputs and run is not None and not failed_calls(run) and all(f.is_file() for f in files)
                and read_json(folder / (stem + INPUT_JSON)) == inputs[stem]):
            continue
        if stem not in inputs:
            dropped += 1
        elif files[0].is_file():                 # run_all.py would have skipped it
            redo += 1
        for f in files:
            f.unlink(missing_ok=True)
    if redo or dropped:
        log(f"indirect results removed: {redo} filing(s) to run again (failed model calls, incomplete results or other "
            f"prepared files), {dropped} of filing(s) no longer prepared")
    return redo


def note_costs(folder: Path, ledger: dict, kept: set[Path] | None = None) -> dict:
    """The model cost ledger of the indirect folder (COSTS_JSON, uploaded with it): the filing and cost of every run
    file this run's task 6 has produced, keyed by the SHA-256 of its content, so the answers that keep_finished removes
    (paid for in this run) stay counted in later attempts. Without kept, the run files in folder were staged from S3,
    and one the ledger has already is not added again. With kept (the run files there before run_all.py), the others
    were written by this attempt and are added, also when an earlier run file had the same content. Writes the ledger
    to folder and returns it."""
    folder.mkdir(parents=True, exist_ok=True)
    ledger = {k: v for k, v in ledger.items() if isinstance(v, dict)}
    for path in sorted(folder.glob("*" + RUN_JSON)):
        if kept is not None and path in kept:
            continue
        data = path.read_bytes()
        try:
            run = json.loads(data)
        except ValueError:                           # unreadable: keep_finished runs the filing again
            continue
        if not isinstance(run, dict):
            continue
        key = hashlib.sha256(data).hexdigest()
        while kept is not None and key in ledger:    # written now: paid for now
            key += "+"
        ledger.setdefault(key, {"filing": path.name.removesuffix(RUN_JSON), "cost_usd": round(run_cost(run), 6)})
    (folder / COSTS_JSON).write_text(json.dumps(ledger, indent=1, sort_keys=True), encoding="utf-8")
    return ledger


def note_inputs(folder: Path, inputs: dict[str, dict[str, str]]) -> None:
    """After run_all.py: the input file of each filing it finished, the prepared files its answers were made from
    (keep_finished removed the input file of every filing that ran again; a kept filing keeps its own)."""
    for run in sorted(folder.glob("*" + RUN_JSON)):
        stem = run.name.removesuffix(RUN_JSON)
        target = folder / (stem + INPUT_JSON)
        if stem in inputs and not target.exists():
            target.write_text(json.dumps(inputs[stem], indent=1, sort_keys=True), encoding="utf-8")


def run_all_counts(tail: list[str]) -> dict:
    """The counts run_all.py prints last, e.g. "done {'ok': 60, 'fail': 1}"."""
    for line in reversed(tail):
        if line.startswith("done {"):
            try:
                return ast.literal_eval(line[len("done "):])
            except (ValueError, SyntaxError):
                return {}
    return {}


_XLSX = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
         "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def sheet_rows(xlsx: Path) -> dict[str, int]:
    """Rows below the header row of each sheet (the last row number - 1, as openpyxl's max_row - 1), read from the
    workbook XML so the runner does not need openpyxl."""
    rows, row_tag = {}, f"{{{_XLSX['m']}}}row"
    with zipfile.ZipFile(xlsx) as z:
        rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
        for sheet in ET.fromstring(z.read("xl/workbook.xml")).iterfind("m:sheets/m:sheet", _XLSX):
            target = rels[sheet.get(f"{{{_XLSX['r']}}}id")]
            part = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
            last = 0
            with z.open(part) as fh:
                for _, el in ET.iterparse(fh):
                    if el.tag == row_tag:
                        last = max(last + 1, int(el.get("r") or 0))     # r is the row number; rows can skip numbers
                        el.clear()
            rows[sheet.get("name")] = max(last - 1, 0)
    return rows
