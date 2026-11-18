#!/usr/bin/env python3
"""Local end-to-end run of the company evidence pipeline on Lungpacer (deploy/e2e/README.md).

The pipeline image runs each task in a fresh container, in the order of the company_evidence DAG, against a local
moto server that stands in for S3 and Secrets Manager. Then the run record, every S3 key of deploy/INTERFACES.md and
the workbook are checked; the workbook against one built from the same filings with the main branch's code.

    python deploy/e2e/run_e2e.py --work DIR --expected EXPECTED.xlsx            # every step
    python deploy/e2e/run_e2e.py --work DIR --expected EXPECTED.xlsx --keep-moto
    python deploy/e2e/run_e2e.py --work DIR --steps build,run,check,compare --run-id RUN_ID --from-task evidence
    python deploy/e2e/run_e2e.py --work DIR --steps stop                          # stop a kept moto server

Steps (--steps, always done in this order):
  build    docker build -f deploy/image/Dockerfile -t IMAGE <repository root>
  moto     moto_server on 127.0.0.1:5055 (--moto-host, --port; CORS off), which containers reach at
           http://host.docker.internal:PORT
  seed     bucket company-reports-data: input/lungpacer/company.json, raw/lungpacer/sec/ (the local dump) and
           reference/ (Form D, DOL, N-PORT); no secret
  run      plan a run as company_intake does (deploy/dags/px_intake.py: decision, run id, queued record), then one
           `docker run --rm IMAGE <task> --company lungpacer --run-id RUN_ID` per task: aliases, finder, records,
           evidence, indirect, publish, finalize. download is not run (no SEC traffic): it is recorded as skipped.
           finder and indirect read the model key: it goes into moto (px/openrouter-api-key, from
           company-finder/.env) right before each of those two containers starts and is deleted as soon as it ends
  check    the run record (every task succeeded, download skipped here, model cost), every S3 key of the contract,
           no secret left in moto, and no secret value in any S3 object, any log or the image
  compare  the workbook in reports/lungpacer/ against EXPECTED (compare_workbooks.py)
  stop     stop the moto server this script started

moto has no authentication: whoever reaches its port can read what it holds. So it listens on one address (never a
wildcard), and an invocation that started or used it stops it when it ends (after the last step, a failure, an
exception, Ctrl-C, SIGTERM or SIGHUP), with its data, unless --keep-moto is given: then it keeps the data (no secret)
for a later invocation, to continue a run (--run-id, --from-task) or for build_expected.py --from-run.

The finder and indirect tasks call OpenRouter for real (Lungpacer: well under 1 USD). SEC_USER_AGENT never goes into
moto. Secret values are never printed or written to a file; check reads both from company-finder/.env only to look
for them in what the run wrote.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import ipaddress
import json
import os
import platform
import re
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path[:0] = [str(HERE), str(REPO / "deploy" / "runner"), str(REPO / "deploy" / "dags")]

CO, BUCKET, REGION = "lungpacer", "company-reports-data", "us-east-2"
SECRET_IDS = {"SEC_USER_AGENT": "px/sec-user-agent", "OPENROUTER_API_KEY": "px/openrouter-api-key"}
TASKS = ("aliases", "download", "finder", "records", "evidence", "indirect", "publish", "finalize")
NOT_RUN = {"download": "not run by the local end-to-end check: the filings are seeded from the local dump, no SEC traffic"}
STEPS = ("build", "moto", "seed", "run", "check", "compare", "stop")
MOTO_STEPS = {"moto", "seed", "run", "check", "compare"}            # steps that start or use the moto server
DATA = REPO.parent                                   # the checkout's data folder (dumps/), as pipeline/paths.py
SOURCES = {                                          # --<name> overrides each one
    "config": REPO / "pipeline" / "config" / f"{CO}.json",
    "companies": REPO / "Resources" / "Targeted_pdf" / "port_v2_run" / "input" / f"{CO}_input.csv",
    "manifest": DATA / "dumps" / f"{CO}_manifest.csv",
    "files": DATA / "dumps" / CO,
    "formd": Path.home() / "projectx_test_archive" / "runs" / CO / "input" / "formd" / "formd.duckdb",
    "dol": DATA / "dumps" / "employee_docs" / "raw",
    "nport": DATA / "dumps" / f"{CO}_nport" / "nport_holdings.csv",
    "env": REPO / "company-finder" / ".env",
}
DOL_FILES = ("form5500_rows.csv", "form5500sf_rows.csv", "schedule_h_rows.csv")
EVIDENCE_FILES = ("parsed_rows.csv", "evidence_rows.csv", "holdings_from_evidence.csv", "holdings_conflicts.csv",
                  "validation_fail.csv", "lookalike_filings.csv", "prose_sentences.csv", "locator.csv")

# Never reach real AWS: dummy credentials, no profile or config file; every client gets the moto endpoint.
for _name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
    os.environ.pop(_name, None)
os.environ.update(AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing", AWS_CONFIG_FILE=os.devnull,
                  AWS_SHARED_CREDENTIALS_FILE=os.devnull, AWS_EC2_METADATA_DISABLED="true", AWS_REGION=REGION)


def say(msg: str = "") -> None:
    print(msg, flush=True)


def fail(msg: str) -> None:
    raise SystemExit(f"e2e: {msg}")


# ---------------------------------------------------------------------- AWS (moto) and local helpers
def moto_url(host: str, port: int) -> str:
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


def is_wildcard(host: str) -> bool:
    """True for an address that listens on every interface (0.0.0.0, ::, 0 ...)."""
    host = host.strip().strip("[]")
    if host in ("", "*"):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:                                 # not resolvable: moto's own start fails with the reason
        return False
    return any(ipaddress.ip_address(info[4][0].split("%")[0]).is_unspecified for info in infos)


def aws(service: str, port: int, host: str = "127.0.0.1"):
    import boto3
    from botocore.config import Config
    return boto3.client(service, endpoint_url=moto_url(host, port), region_name=REGION,
                        config=Config(retries={"mode": "standard", "max_attempts": 5}))


def listing(s3, prefix: str = "") -> dict[str, dict]:
    """key -> {"size", "etag"} of every object under prefix."""
    out = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        for o in page.get("Contents", []):
            out[o["Key"]] = {"size": o["Size"], "etag": o["ETag"].strip('"')}
    return out


def get_bytes(s3, key: str) -> bytes:
    return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()


def load_record(s3, run_id: str) -> dict:
    return json.loads(get_bytes(s3, f"runs/{CO}/{run_id}.json"))


def local_files(folder: Path) -> list[tuple[str, Path]]:
    """(relative POSIX path, path) of every file under folder, following symlinks; macOS .DS_Store left out."""
    out = []
    for root, dirs, names in os.walk(folder, followlinks=True):
        dirs.sort()
        for n in sorted(names):
            if n != ".DS_Store":
                p = Path(root) / n
                out.append((p.relative_to(folder).as_posix(), p))
    return out


def read_env_secrets(path: Path, names=tuple(SECRET_IDS)) -> dict[str, str]:
    """The values of names (default SEC_USER_AGENT and OPENROUTER_API_KEY) from KEY=value lines (quotes stripped, as
    the pipeline reads them). The values are returned to the caller only: never printed, logged or written."""
    if not path.is_file():
        fail(f"{path} not found (--env)")
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("export "):
            s = s[7:].lstrip()
        key, sep, value = s.partition("=")
        if sep and key.strip() in names and not s.startswith("#"):
            out[key.strip()] = value.strip().strip("'\"")
    return out


def run_dir(a, run_id: str) -> Path:
    d = a.work / "runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def docker_ok() -> None:
    r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True)
    if r.returncode:
        fail("the Docker daemon is not reachable (start Docker Desktop or dockerd): " + (r.stderr.strip()[:300]))


# ---------------------------------------------------------------------- build
def step_build(a) -> None:
    docker_ok()
    log = a.work / "logs" / "build.log"
    cmd = ["docker", "build", "-f", str(REPO / "deploy" / "image" / "Dockerfile"), "-t", a.image, str(REPO)]
    say("== build\n$ " + " ".join(cmd) + f"\n   (output: {log})")
    t0 = time.monotonic()
    with open(log, "w", encoding="utf-8") as fh:
        code = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT).returncode
    if code:
        say("".join(log.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)[-40:]))
        fail(f"docker build failed (exit {code}); full output in {log}")
    info = json.loads(subprocess.run(["docker", "image", "inspect", a.image], capture_output=True, text=True,
                                     check=True).stdout)[0]
    say(f"built {a.image}: {info['Id'][:19]}, {info['Architecture']}, {info['Size'] / 1e6:.0f} MB, "
        f"{time.monotonic() - t0:.0f}s")


# ---------------------------------------------------------------------- moto server
def moto_alive(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with urllib.request.urlopen(f"{moto_url(host, port)}/moto-api/", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def pid_alive(pid: int) -> bool:
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:  # a child of this process that has ended (reaped here)
            return False
    except ChildProcessError:                      # started by an earlier invocation: not a child of this one
        pass
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def our_moto(a) -> tuple[int, int] | None:
    """(pid, port) of the moto server this script started for --work, while it runs. The pid must still be a
    moto_server process, so a pid the system has given to another process since is never signalled."""
    try:
        pid, port = (int(x) for x in (a.work / "moto.pid").read_text().split()[:2])
    except (OSError, ValueError):
        return None
    command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    return (pid, port) if "moto_server" in command and pid_alive(pid) else None


def step_moto(a) -> None:
    say(f"== moto server, port {a.port}")
    running = our_moto(a)
    if running and running[1] != a.port:
        fail(f"this script's moto server runs on port {running[1]} (pid {running[0]}): pass --port {running[1]}, "
             f"or stop it first (--steps stop)")
    if moto_alive(a.port, a.moto_host):
        if running:
            say(f"reusing the moto server started earlier (pid {running[0]}); its data is kept")
            return
        fail(f"a moto server that this script did not start answers on port {a.port}: pass another --port")
    with socket.socket() as sock:
        try:
            sock.bind((a.moto_host, a.port))
        except OSError as e:
            fail(f"port {a.port} on {a.moto_host} is in use ({e}): pass another --port")
    exe = Path(sys.executable).with_name("moto_server")
    if not exe.exists():
        fail(f"{exe} not found: run this script with the Python of a venv that has deploy/e2e/requirements.txt")
    tmp = a.work / "moto_tmp"                        # moto spools large objects to temporary files: keep them here
    tmp.mkdir(exist_ok=True)
    # no secret of this shell reaches moto. CORS off stops cross-origin reads by web pages, not a page that rebinds its
    # own host name to 127.0.0.1 (moto answers any Host header), nor local processes or containers: the model key is
    # in moto only while finder or indirect runs (secret_in_moto)
    env = {k: v for k, v in os.environ.items() if k not in SECRET_IDS}
    env.update(TMPDIR=str(tmp), MOTO_DISABLE_GLOBAL_CORS="true")
    with open(a.work / "logs" / "moto.log", "ab") as log:
        proc = subprocess.Popen([str(exe), "-H", a.moto_host, "-p", str(a.port)], stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env, start_new_session=True)
    (a.work / "moto.pid").write_text(f"{proc.pid} {a.port}\n")
    for _ in range(80):
        if moto_alive(a.port, a.moto_host):
            say(f"moto server pid {proc.pid} on {a.moto_host}:{a.port} (log: {a.work / 'logs' / 'moto.log'}); "
                f"containers use http://host.docker.internal:{a.port}")
            return
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    fail(f"the moto server did not start; see {a.work / 'logs' / 'moto.log'}")


def step_stop(a) -> None:
    running = our_moto(a)
    if not running:
        (a.work / "moto.pid").unlink(missing_ok=True)
        say("== stop: no moto server of this script is running")
        return
    pid, port = running
    for sig in (signal.SIGTERM, signal.SIGKILL):     # SIGKILL only if it has not stopped 10 s after SIGTERM
        if pid_alive(pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, sig)
            for _ in range(40):
                if not pid_alive(pid):
                    break
                time.sleep(0.25)
    if pid_alive(pid):
        fail(f"moto server pid {pid} did not stop: stop it by hand (kill -9 {pid})")
    (a.work / "moto.pid").unlink(missing_ok=True)
    say(f"== stop: moto server pid {pid} on port {port} stopped (its data is gone)")


def delete_secrets(sm, ids) -> bool:
    """Delete these secrets from moto at once, without a recovery window (a missing one is fine). False when one
    could not be deleted."""
    done = True
    for sid in ids:
        try:
            sm.delete_secret(SecretId=sid, ForceDeleteWithoutRecovery=True)
        except sm.exceptions.ResourceNotFoundException:
            pass
        except Exception as exc:                 # moto unreachable: the secret goes when moto stops
            done = False
            say(f"WARNING: secret {sid} was not deleted from moto ({type(exc).__name__}); stop moto (--steps stop)")
    return done


@contextlib.contextmanager
def secret_in_moto(a, sm, task: str):
    """The secret task reads (the runner's tasks.TASK_SECRET), in moto only while the task's container runs: read from
    the env file and stored right before, deleted right after, also when the task fails or the script is
    interrupted. A task that reads no secret gets none."""
    from px_runner import tasks
    name = tasks.TASK_SECRET.get(task)
    if not name:
        yield
        return
    sid, env_file = SECRET_IDS[name], a.sources["env"]
    value = read_env_secrets(env_file, (name,)).get(name)
    if not value:
        fail(f"{env_file} has no value for {name}, which task {task} reads")
    try:
        delete_secrets(sm, [sid])                # one left by an invocation that was killed while a task ran
        sm.create_secret(Name=sid, SecretString=value)
        value = None
        say(f"   secret {sid}: set in moto from {env_file} for {task} only (value not shown)")
        yield
    finally:
        if delete_secrets(sm, [sid]):
            say(f"   secret {sid}: deleted from moto")


def check_run_secrets(a) -> None:
    """Before any step: the env file has a value for each secret that the tasks to run read (values not kept)."""
    from px_runner import tasks
    first = TASKS.index(a.from_task) if a.from_task else 0
    names = sorted({tasks.TASK_SECRET[t] for t in TASKS[first:-1] if t in tasks.TASK_SECRET and t not in NOT_RUN})
    if names:
        found = read_env_secrets(a.sources["env"], names)
        absent = [n for n in names if not found.get(n)]
        found.clear()
        if absent:
            fail(f"{a.sources['env']} has no value for {', '.join(absent)}")


# ---------------------------------------------------------------------- seed
def company_json(src: dict) -> bytes:
    """The pipeline config plus run_indirect and the finder's look-alike inputs company.legal_name and
    company.description, taken from the companies file the current workbook's finder run used."""
    cfg = json.loads(src["config"].read_text(encoding="utf-8"))
    company = dict(cfg.get("company") or {})
    cik = re.sub(r"\D", "", str(company.get("cik") or ""))
    with open(src["companies"], newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    row = next((r for r in rows if cik and (r.get("id") or "").strip() == f"C{int(cik):010d}"), None)
    if row is None:
        fail(f"{src['companies']} has no row with id C{cik.zfill(10)}")
    legal, description = (row.get("canonical_name") or "").strip(), (row.get("description") or "").strip()
    if not legal or not description:
        fail(f"{src['companies']}: canonical_name or description is empty")
    cfg["company"] = {"legal_name": legal, "description": description,
                      **{k: v for k, v in company.items() if k not in ("legal_name", "description")}}
    cfg["run_indirect"] = True
    return (json.dumps(cfg, indent=1, ensure_ascii=False) + "\n").encode("utf-8")


def upload_files(s3, pairs: list[tuple[str, Path]], threads: int = 8) -> tuple[int, int]:
    """Upload (key, path) pairs, skipping objects already there with the same size. Returns (uploaded, bytes)."""
    have = {}
    for prefix in sorted({k.rsplit("/", 1)[0] + "/" if "/" in k else "" for k, _ in pairs}):
        have.update({k: v["size"] for k, v in listing(s3, prefix).items()})
    todo = [(k, p) for k, p in pairs if have.get(k) != p.stat().st_size]
    with ThreadPoolExecutor(threads) as pool:
        list(pool.map(lambda kp: s3.upload_file(str(kp[1]), BUCKET, kp[0]), todo))
    return len(todo), sum(p.stat().st_size for _, p in todo)


def step_seed(a, s3) -> None:
    """The input file, the raw filings and the reference data. No secret: run stores the model key only while a task
    that reads it runs."""
    src = a.sources
    say(f"== seed s3://{BUCKET} (copies; the originals are only read)")
    missing = [f"{k}: {p}" for k, p in src.items() if k != "env" and not p.exists()]
    if missing:
        fail("missing sources:\n  " + "\n  ".join(missing))
    try:
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    body = company_json(src)
    (a.work / "seed").mkdir(exist_ok=True)
    (a.work / "seed" / "company.json").write_bytes(body)
    s3.put_object(Bucket=BUCKET, Key=f"input/{CO}/company.json", Body=body, ContentType="application/json")
    cfg = json.loads(body)
    say(f"input/{CO}/company.json: {src['config'].name} + run_indirect true, legal_name {cfg['company']['legal_name']!r}, "
        f"description ({len(cfg['company']['description'])} characters) from {src['companies'].name}")

    files = local_files(src["files"])
    pairs = [(f"raw/{CO}/sec/manifest.csv", src["manifest"])] + [(f"raw/{CO}/sec/files/{rel}", p) for rel, p in files]
    pairs += [("reference/formd/formd.duckdb", src["formd"]), (f"reference/nport/{CO}/nport_holdings.csv", src["nport"])]
    pairs += [(f"reference/dol/raw/{n}", src["dol"] / n) for n in DOL_FILES]
    welfare = src["dol"].parent / "data" / "welfare_plans_by_year.csv"
    if welfare.exists():                             # optional in the contract; not produced today
        pairs.append(("reference/dol/data/welfare_plans_by_year.csv", welfare))
    t0 = time.monotonic()
    n, size = upload_files(s3, pairs)
    with open(src["manifest"], newline="", encoding="utf-8") as fh:
        manifest_rows = sum(1 for _ in csv.DictReader(fh))
    say(f"raw/{CO}/sec/: manifest ({manifest_rows} filings) and {len(files)} files "
        f"({sum(p.stat().st_size for _, p in files) / 1e6:.0f} MB) from {src['files']}")
    say(f"reference/: formd/formd.duckdb, nport/{CO}/nport_holdings.csv, dol/raw/ ({', '.join(DOL_FILES)})"
        + (", dol/data/welfare_plans_by_year.csv" if welfare.exists() else "; no welfare_plans_by_year.csv (optional)"))
    say(f"uploaded {n} new or changed object(s), {size / 1e6:.0f} MB, in {time.monotonic() - t0:.0f}s")
    say("no secret seeded: the run step stores the model key only while finder or indirect runs")
    (a.work / "seed" / "seed.json").write_text(json.dumps(
        {"raw_files": len(files), "manifest_rows": manifest_rows,
         "sources": {k: str(v) for k, v in src.items() if k != "env"}}, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------- run
def plan_run(s3) -> str:
    """A new run as company_intake plans one: its decision, the run id and the queued record."""
    import px_intake
    inputs = px_intake.list_inputs(s3, BUCKET)
    if CO not in inputs:
        fail(f"s3://{BUCKET}/input/{CO}/company.json is missing: run the seed step")
    now = datetime.now(timezone.utc)
    latest = px_intake.read_latest_records(s3, BUCKET, [CO])
    d = px_intake.decide({CO: inputs[CO]}, latest, now, refresh_days=7, stale_hours=24)[0]
    if d.start:
        plan = d.to_plan()
        say(f"intake decision: start ({d.reason}: {d.detail}); run {plan['run_id']}")
    else:
        run_id = px_intake.make_run_id(now, inputs[CO])
        plan = {"company": CO, "run_id": run_id, "input_etag": px_intake.normalize_etag(inputs[CO]),
                "created_at": px_intake.iso_z(now)}
        say(f"intake decision: no new run ({d.reason}: {d.detail}); starting run {run_id} by hand, "
            "as a manual trigger with a new run id would")
    px_intake.put_record(s3, BUCKET, px_intake.queued_record(plan))
    return plan["run_id"]


def record_not_run(s3, run_id: str, task: str) -> None:
    """A task this check does not run, recorded the way the runner records a skipped task."""
    from px_runner import runrecord, s3sync
    bucket = s3sync.Bucket(s3, BUCKET)
    started = runrecord.now()
    runrecord.start_task(bucket, CO, run_id, task, started, f"input/{CO}/company.json")
    runrecord.finish_task(bucket, CO, run_id, task, "skipped", started, 0.0, {"reason": NOT_RUN[task]})


def container_cmd(a, task: str, run_id: str) -> tuple[str, list[str]]:
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", f"px-e2e-{CO}-{task}-{run_id}")
    cmd = ["docker", "run", "--rm", "--name", name]
    if platform.system() == "Linux":                 # Docker Desktop resolves host.docker.internal by itself
        cmd += ["--add-host", "host.docker.internal:host-gateway"]
    env = {"AWS_ENDPOINT_URL": f"http://host.docker.internal:{a.port}", "AWS_REGION": REGION,
           "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "PX_BUCKET": BUCKET}
    if a.workers:
        env["PX_WORKERS"] = str(a.workers)
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    return name, cmd + [a.image, task, "--company", CO, "--run-id", run_id]


def run_container(a, task: str, run_id: str, log_path: Path) -> tuple[int, float]:
    """One task in a fresh container; output to the log file and (all of it, or with --quiet the runner's own
    lines) to the console. Returns (exit code, seconds including the container's start and stop)."""
    name, cmd = container_cmd(a, task, run_id)
    say("$ " + " ".join(cmd))
    t0 = time.monotonic()
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace")
        try:
            for line in proc.stdout:
                log.write(line)
                log.flush()
                if not a.quiet or line.startswith("[px_runner]"):
                    sys.stdout.write(f"  {task} | {line}")
                    sys.stdout.flush()
            code = proc.wait()
        except KeyboardInterrupt:                    # SIGTERM: the runner records the task as failed
            subprocess.run(["docker", "stop", "-t", "30", name], capture_output=True)
            proc.wait()
            raise
    return code, time.monotonic() - t0


def step_run(a, s3, sm) -> tuple[str, bool]:
    docker_ok()
    run_id = a.run_id or plan_run(s3)
    (a.work / "last_run_id").write_text(run_id + "\n")
    rdir = run_dir(a, run_id)
    image_id = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", a.image], capture_output=True,
                              text=True).stdout.strip()
    if not image_id:
        fail(f"image {a.image} not found: run the build step")
    say(f"== run {CO} {run_id} with {a.image} ({image_id[:19]}); logs in {rdir}")
    timing_path = rdir / "timings.json"
    timings = json.loads(timing_path.read_text()) if timing_path.exists() else {}
    first = TASKS.index(a.from_task) if a.from_task else 0
    failed = None
    for task in TASKS[first:-1]:
        if task in NOT_RUN:
            record_not_run(s3, run_id, task)
            say(f"== {task}: recorded as skipped ({NOT_RUN[task]})")
            continue
        say(f"== {task}")
        with secret_in_moto(a, sm, task):           # the model key for finder and indirect, nothing for the others
            code, secs = run_container(a, task, run_id, rdir / f"{task}.log")
        entry = load_record(s3, run_id)["tasks"].get(task) or {}
        timings[task] = {"exit": code, "wall_seconds": round(secs, 1), "image": image_id}
        timing_path.write_text(json.dumps(timings, indent=1))
        say(f"== {task}: exit {code}, {secs:.0f}s with the container; run record: {entry.get('status')}, "
            f"{entry.get('seconds')}s, outputs {json.dumps(entry.get('outputs', {}))[:400]}")
        if code:
            failed = task
            say(f"== {task} FAILED; the record's error:\n{entry.get('error', '(none recorded)')}")
            break
    say("== finalize")                               # always, as the DAG's trigger_rule=all_done
    code, secs = run_container(a, "finalize", run_id, rdir / "finalize.log")
    timings["finalize"] = {"exit": code, "wall_seconds": round(secs, 1), "image": image_id}
    timing_path.write_text(json.dumps(timings, indent=1))
    rec = load_record(s3, run_id)
    (rdir / "run_record.json").write_text(json.dumps(rec, indent=1), encoding="utf-8")
    say(f"\nrun {run_id}: {rec['status']}; model cost {rec['model_cost_usd']}")
    say(f"{'task':10} {'status':10} {'record s':>9} {'with container s':>17}")
    for task in TASKS[:-1]:
        e = rec["tasks"].get(task) or {}
        w = timings.get(task, {}).get("wall_seconds", "")
        say(f"{task:10} {e.get('status', '-'):10} {e.get('seconds', ''):>9} {w:>17}")
    if failed and a.keep_moto:
        say(f"\nfix the cause, rebuild if deploy/ changed, then continue the same run (moto keeps its data):\n  "
            f"{sys.executable} {__file__} --work {a.work} --port {a.port} --steps build,run,check,compare "
            f"--run-id {run_id} --from-task {failed} --keep-moto" + (f" --expected {a.expected}" if a.expected else "")
            + f"\nand stop moto when done: {sys.executable} {__file__} --work {a.work} --steps stop")
    elif failed:
        say("\nthe moto server stops now, and this run's data with it: to continue a failed run after a fix instead "
            "of starting again, run with --keep-moto")
    # finalize exits 0 once it has recorded the run, failed or not: the record says how the run ended
    return run_id, failed is None and code == 0 and rec["status"] == "succeeded"


# ---------------------------------------------------------------------- check
def scan_for(needles: list[bytes], data: bytes) -> bool:
    return any(n in data for n in needles)


def secret_needles(src: dict) -> list[bytes]:
    """The secret values (and their URL-encoded forms) as bytes, to look for; never printed."""
    out = set()
    for v in read_env_secrets(src["env"]).values():
        if len(v) >= 8:
            out.update(x.encode("utf-8") for x in (v, urllib.parse.quote(v), urllib.parse.quote_plus(v)))
    return sorted(out)


def scan_image(image: str, needles: list[bytes]) -> tuple[int, int, list[str]]:
    """Every regular file of the image (docker export of a created, never started container) and its metadata.
    Returns (files, bytes, paths that hold a secret value)."""
    hits, files, size = [], 0, 0
    meta = subprocess.run(["docker", "image", "inspect", image], capture_output=True, check=True).stdout
    meta += subprocess.run(["docker", "history", "--no-trunc", image], capture_output=True, check=True).stdout
    if scan_for(needles, meta):
        hits.append("(image config or history)")
    cid = subprocess.run(["docker", "create", image], capture_output=True, text=True, check=True).stdout.strip()
    try:
        proc = subprocess.Popen(["docker", "export", cid], stdout=subprocess.PIPE)
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
            for m in tf:
                if m.isfile():
                    data = tf.extractfile(m).read()
                    files, size = files + 1, size + len(data)
                    if scan_for(needles, data):
                        hits.append(m.name)
        proc.wait()
    finally:
        subprocess.run(["docker", "rm", cid], capture_output=True)
    return files, size, hits


def step_check(a, s3, sm, run_id: str) -> bool:
    say(f"== check run {run_id}")
    rec = load_record(s3, run_id)
    rdir = run_dir(a, run_id)
    (rdir / "run_record.json").write_text(json.dumps(rec, indent=1), encoding="utf-8")
    results: list[tuple[bool, str, str]] = []

    def check(cond, what: str, detail: str = "") -> None:
        results.append((bool(cond), what, detail))

    # the run record
    check(rec.get("status") == "succeeded", "run status succeeded", str(rec.get("status")))
    for f in ("created_at", "started_at", "ended_at"):
        check(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", str(rec.get(f))), f"run {f} set (UTC, Z)", str(rec.get(f)))
    etag = s3.head_object(Bucket=BUCKET, Key=f"input/{CO}/company.json")["ETag"].strip('"')
    check(rec.get("input_etag") == etag and run_id.endswith("-" + etag[:8]), "input_etag = the input's ETag; run id ends with its first 8 characters",
          f"{rec.get('input_etag')}")
    for t in TASKS[:-1]:
        e = rec["tasks"].get(t) or {}
        want = "skipped" if t in NOT_RUN else "succeeded"
        check(e.get("status") == want and "error" not in e, f"task {t} {want}" + (" (here)" if t in NOT_RUN else ""),
              f"{e.get('status')}, {e.get('seconds')}s")
    cost = (rec.get("model_cost_usd") or {}).get("indirect")
    check(isinstance(cost, (int, float)) and cost > 0, "model cost of indirect recorded", f"{cost} USD")
    check(cost == (rec["tasks"].get("indirect") or {}).get("outputs", {}).get("model_cost_usd"),
          "model cost at the top = indirect outputs", "")
    check(rec.get("workbook") and rec["workbook"].startswith(f"reports/{CO}/{run_id}/"), "workbook key recorded",
          str(rec.get("workbook")))

    # every key of the contract's S3 table
    objs = listing(s3)
    i = f"intermediate/{CO}/{run_id}/"
    book = (rec.get("workbook") or "").rsplit("/", 1)[-1]
    keys = [("input", f"input/{CO}/company.json"), ("raw manifest", f"raw/{CO}/sec/manifest.csv"),
            ("task 1 companies file", f"{i}mentions/input/{CO}_input.csv")]
    keys += [(f"task 3 finder {s or 'per filing'}", f"{i}mentions/out/{CO}/{CO}{s}.csv")
             for s in ("", "_mentions", "_dropped", "_summary")]
    keys += [("task 4 Form D offerings", f"{i}records/formd_offerings.csv"),
             ("task 4 DOL rows", f"{i}records/inputs/data/dol_form5500.csv"),
             ("task 4 401(k) participants", f"{i}records/inputs/data/plan_participants.csv"),
             ("task 4 N-PORT price marks", f"{i}records/inputs/data/nport_price_monthly.csv"),
             ("task 4 N-PORT holdings", f"{i}records/nport/nport_holdings.csv")]
    keys += [(f"task 5 evidence {n}", f"{i}evidence/{n}") for n in EVIDENCE_FILES]
    keys += [("task 6 run log", f"{i}indirect/run.log"), ("task 7 located indirect mentions", f"{i}publish/indirect_located.csv"),
             ("workbook of the run", f"reports/{CO}/{run_id}/{book}"), ("workbook latest", f"reports/{CO}/latest/{book}"),
             ("run record", f"runs/{CO}/{run_id}.json"), ("reference Form D", "reference/formd/formd.duckdb"),
             ("reference N-PORT", f"reference/nport/{CO}/nport_holdings.csv")]
    keys += [(f"reference DOL {n}", f"reference/dol/raw/{n}") for n in DOL_FILES]
    for label, key in keys:
        o = objs.get(key)
        check(o is not None and (o["size"] > 0 or key.endswith(".csv")), f"{label}: {key}",
              f"{o['size']} bytes" if o else "MISSING")

    def under(prefix: str, suffix: str = "") -> list[str]:
        return [k for k in objs if k.startswith(prefix) and k.endswith(suffix)]

    seeded = json.loads((a.work / "seed" / "seed.json").read_text()) if (a.work / "seed" / "seed.json").exists() else {}
    raw = under(f"raw/{CO}/sec/files/")
    check(raw and len(raw) == seeded.get("raw_files", len(raw)), "raw files: as seeded (download not run)", f"{len(raw)} files")
    out = {t: (rec["tasks"].get(t) or {}).get("outputs", {}) for t in TASKS}
    metas = under(f"{i}prepared/companies/", ".meta.json")
    check(metas and len(metas) == out["evidence"].get("prepared_filings"), "task 5 prepared filings (companies/*.meta.json)",
          f"{len(metas)}, record {out['evidence'].get('prepared_filings')}")
    check(len(under(f"{i}prepared/companies/", "_mentions.csv")) == len(metas), "task 5 one mentions file per prepared filing",
          f"{len(under(f'{i}prepared/companies/', '_mentions.csv'))}")
    pdfs = under(f"{i}prepared/pdfs/")
    check(len(pdfs) == len(metas), "task 5 prepared/pdfs: one copied filing per prepared filing", f"{len(pdfs)}")
    runs = under(f"{i}indirect/", "_indirect_run.json")
    check(len(runs) == len(metas) == out["indirect"].get("filings"), "task 6 one *_indirect_run.json per prepared filing",
          f"{len(runs)}, record {out['indirect'].get('filings')}")
    inputs = under(f"{i}indirect/", "_indirect_input.json")
    check(len(inputs) == len(runs), "task 6 one *_indirect_input.json (the prepared files used) per run file",
          f"{len(inputs)}")
    ledger = json.loads(get_bytes(s3, f"{i}indirect/model_cost.json")) if f"{i}indirect/model_cost.json" in objs else {}
    check(ledger and round(sum(e.get("cost_usd") or 0 for e in ledger.values()), 4) == cost,
          "task 6 model_cost.json adds up to the recorded cost", f"{len(ledger)} run file(s)")
    check(out["indirect"].get("failed_filings") == 0, "task 6 no failed filing", str(out["indirect"].get("failed_filings")))
    latest = under(f"reports/{CO}/latest/")
    run_book = objs.get(f"reports/{CO}/{run_id}/{book}", {})
    check(len(latest) == 1 and objs[latest[0]]["etag"] == run_book.get("etag"), "latest/ holds only this run's workbook",
          ", ".join(latest))
    for task, files in ((t, out[t].get("files_uploaded")) for t in ("aliases", "finder", "records", "evidence", "indirect")):
        check(isinstance(files, int) and files > 0, f"task {task} uploaded files", str(files))

    # no secret left in moto once the containers are done, no secret value anywhere the run wrote to, nor in the image
    left = [s["Name"] for page in sm.get_paginator("list_secrets").paginate() for s in page.get("SecretList", [])]
    check(not left, "no secret left in the moto server", ", ".join(left) or "none")
    needles = secret_needles(a.sources)
    check(needles, "secret values read from the env file for the scan", f"{len(needles)} forms")
    t0, hits, scanned = time.monotonic(), [], 0
    for key in sorted(objs):
        data = get_bytes(s3, key)
        scanned += len(data)
        if scan_for(needles, data):
            hits.append(key)
    check(not hits, "no secret value in any S3 object", f"{len(objs)} objects, {scanned / 1e6:.0f} MB, "
          f"{time.monotonic() - t0:.0f}s" + (f"; FOUND IN {hits}" if hits else ""))
    logs = [p for p in a.work.rglob("*.log") if p.is_file()]
    bad = [str(p) for p in logs if scan_for(needles, p.read_bytes())]
    check(not bad, "no secret value in the task, build and moto logs", f"{len(logs)} logs" + (f"; FOUND IN {bad}" if bad else ""))
    if a.image_scan:
        t0 = time.monotonic()
        n, size, found = scan_image(a.image, needles)
        check(not found, "no secret value in the image (files, config, history)",
              f"{n} files, {size / 1e6:.0f} MB, {time.monotonic() - t0:.0f}s" + (f"; FOUND IN {found}" if found else ""))
    needles.clear()

    width = max(len(w) for _, w, _ in results)
    for good, what, detail in results:
        say(f"{'ok  ' if good else 'FAIL'} {what:{width}}  {detail}")
    passed = all(g for g, _, _ in results)
    say(f"check: {sum(g for g, _, _ in results)} of {len(results)} passed")
    (rdir / "check.json").write_text(json.dumps([{"ok": g, "check": w, "detail": d} for g, w, d in results], indent=1),
                                     encoding="utf-8")
    return passed


# ---------------------------------------------------------------------- compare
def step_compare(a, s3, run_id: str) -> bool:
    import compare_workbooks
    from openpyxl import load_workbook

    say(f"== compare run {run_id}")
    rec = load_record(s3, run_id)
    key = rec.get("workbook")
    if not key:
        fail("the run record has no workbook")
    name = key.rsplit("/", 1)[1]
    rdir = run_dir(a, run_id)
    got = rdir / name
    data, latest = get_bytes(s3, key), get_bytes(s3, f"reports/{CO}/latest/{name}")
    got.write_bytes(data)
    say(f"workbook {key} -> {got}; reports/{CO}/latest/{name} is {'the same file' if data == latest else 'A DIFFERENT FILE'}")
    wb = load_workbook(got, read_only=True)
    rows = {ws.title: max((ws.max_row or 1) - 1, 0) for ws in wb.worksheets}
    wb.close()
    recorded = (rec["tasks"].get("publish") or {}).get("outputs", {}).get("sheets", {})
    say(f"sheet rows {rows}; the run record's publish outputs {'agree' if recorded == rows else f'DIFFER: {recorded}'}")
    if not a.expected or not a.expected.is_file():
        say(f"no expected workbook ({a.expected}): pass --expected; comparison not done")
        return False
    report = compare_workbooks.compare(a.expected, got, a.examples)
    text = compare_workbooks.render(report)
    (rdir / "compare.txt").write_text(text + "\n", encoding="utf-8")
    (rdir / "compare.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    say(text)
    say(f"(report: {rdir / 'compare.txt'}, {rdir / 'compare.json'})")
    return report["identical"] and data == latest and recorded == rows


# ---------------------------------------------------------------------- main
class Interrupted(KeyboardInterrupt):
    """SIGTERM or SIGHUP, handled as Ctrl-C: the running container is stopped, then the moto server."""


def _interrupted(signum, frame):
    raise Interrupted(signal.Signals(signum).name)


def end_moto(a) -> None:
    """The end of an invocation that started or used moto, however it ends: moto is stopped unless --keep-moto. Signals
    are ignored from here on, so a second Ctrl-C cannot leave it running."""
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
    running = our_moto(a)
    if running and a.keep_moto:
        if moto_alive(a.port, a.moto_host):
            delete_secrets(aws("secretsmanager", a.port, a.moto_host), SECRET_IDS.values())
        say(f"== moto keeps running (--keep-moto): pid {running[0]}, port {running[1]}, with this run's data and no "
            f"secret; stop it with: {sys.executable} {__file__} --work {a.work} --steps stop")
    elif running:
        step_stop(a)


def run_steps(a, steps: list[str]) -> int:
    if "run" in steps:
        check_run_secrets(a)                         # before the slow steps: the env file has the key the run needs
    if "build" in steps:
        step_build(a)
    if "moto" in steps:
        step_moto(a)
    run_id, rc = a.run_id, 0
    if {"seed", "run", "check", "compare"} & set(steps):
        if not moto_alive(a.port, a.moto_host):
            fail(f"no moto server on port {a.port}: add the moto step (its data is lost when it stops: seed again)")
        s3, sm = aws("s3", a.port, a.moto_host), aws("secretsmanager", a.port, a.moto_host)
        if "seed" in steps:
            step_seed(a, s3)
        if "run" in steps:
            run_id, ok = step_run(a, s3, sm)
            if not ok:
                say("run failed: check and compare skipped")
                return 1
        if {"check", "compare"} & set(steps) and not run_id:
            last = a.work / "last_run_id"
            run_id = last.read_text().strip() if last.exists() else fail("no run id: pass --run-id")
        if "check" in steps and not step_check(a, s3, sm, run_id):
            rc = 1
        if "compare" in steps and not step_compare(a, s3, run_id):
            rc = 1
    if "stop" in steps:
        step_stop(a)
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("--work", type=Path, default=Path(tempfile.gettempdir()) / "px-e2e",
                    help="scratch folder: logs, the seeded company.json, run records and workbooks (default: %(default)s)")
    ap.add_argument("--steps", default="build,moto,seed,run,check,compare", help="comma-separated, from: " + ",".join(STEPS))
    ap.add_argument("--image", default="px-pipeline:dev")
    ap.add_argument("--port", type=int, default=5055, help="moto server port (default %(default)s)")
    ap.add_argument("--moto-host", default="127.0.0.1",
                    help="the one address moto listens on (it has no authentication): 127.0.0.1, which Docker Desktop "
                         "reaches through host.docker.internal; on Linux the docker0 gateway address. Never a wildcard")
    ap.add_argument("--keep-moto", action="store_true",
                    help="leave the moto server running, with its data and no secret, when this invocation ends (to "
                         "continue a run, or for build_expected.py --from-run); --steps stop stops it")
    ap.add_argument("--run-id", help="continue this run instead of planning a new one (with --from-task)")
    ap.add_argument("--from-task", choices=TASKS[:-1], help="first task to run (default: aliases)")
    ap.add_argument("--workers", type=int, help="PX_WORKERS for the containers (default: the runner's, 4)")
    ap.add_argument("--expected", type=Path, help="the workbook to compare with")
    ap.add_argument("--examples", type=int, default=25, help="differences listed per sheet")
    ap.add_argument("--no-image-scan", dest="image_scan", action="store_false", help="skip the image's secret scan")
    ap.add_argument("--quiet", action="store_true", help="console: only the runner's own lines (logs keep everything)")
    for k, v in SOURCES.items():
        ap.add_argument(f"--{k}", type=Path, default=v, help=f"source: default {v}")
    a = ap.parse_args(argv)
    steps = [s.strip() for s in a.steps.split(",") if s.strip()]
    if unknown := sorted(set(steps) - set(STEPS)):
        ap.error(f"unknown step(s) {unknown}; steps are {', '.join(STEPS)}")
    if a.from_task and not a.run_id:
        ap.error("--from-task needs --run-id")
    if is_wildcard(a.moto_host):
        ap.error("--moto-host must be one address, not a wildcard: moto has no authentication")
    a.sources = {k: getattr(a, k) for k in SOURCES}
    a.work = a.work.resolve()
    (a.work / "logs").mkdir(parents=True, exist_ok=True)
    os.environ["AWS_ENDPOINT_URL"] = moto_url(a.moto_host, a.port)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _interrupted)
    try:
        return run_steps(a, steps)
    except KeyboardInterrupt as exc:                 # Ctrl-C, SIGTERM, SIGHUP
        say(f"\n== interrupted ({str(exc) or 'Ctrl-C'})")
        return 130
    finally:
        if MOTO_STEPS & set(steps):
            end_moto(a)


if __name__ == "__main__":
    sys.exit(main())
