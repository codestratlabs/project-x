"""The run record runs/<co>/<run_id>.json (deploy/INTERFACES.md).

The intake DAG creates it (status queued, input_etag, created_at). The first task sets running and started_at, and
creates the record when it is missing. Every task writes only its own entry under "tasks" (tasks of one run never
overlap, so read-update-write is safe). finalize sets the overall status and ended_at. Times are UTC ISO 8601 with Z.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

PIPELINE = ("aliases", "download", "finder", "records", "evidence", "indirect", "publish")
DONE = ("succeeded", "skipped")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def key(co: str, run_id: str) -> str:
    return f"runs/{co}/{run_id}.json"


def blank(co: str, run_id: str, input_etag: str | None = None) -> dict:
    return {"company": co, "run_id": run_id, "status": "queued", "input_etag": input_etag, "created_at": now(),
            "started_at": None, "ended_at": None, "tasks": {}, "model_cost_usd": {"finder": None, "indirect": None},
            "workbook": None}


def load(bucket, co: str, run_id: str) -> dict | None:
    got = bucket.read(key(co, run_id))
    if got is None:
        return None
    rec = {**blank(co, run_id), **json.loads(got[0])}            # the intake DAG writes only some of the fields
    rec["tasks"] = rec.get("tasks") or {}
    rec["model_cost_usd"] = {"finder": None, "indirect": None, **(rec.get("model_cost_usd") or {})}
    return rec


def save(bucket, rec: dict) -> None:
    bucket.write(key(rec["company"], rec["run_id"]), json.dumps(rec, indent=1).encode("utf-8"))


def start_task(bucket, co: str, run_id: str, task: str, started_at: str, input_key: str) -> dict:
    """The run is running (the record is created when missing, with the input file's current ETag) and so is the task."""
    rec = load(bucket, co, run_id) or blank(co, run_id, bucket.etag(input_key))
    if rec["status"] != "running":                                # queued, or a task re-run after finalize
        rec.update(status="running", ended_at=None)
    rec["started_at"] = rec["started_at"] or started_at
    rec["tasks"][task] = {"status": "running", "started_at": started_at}
    save(bucket, rec)
    return rec


def finish_task(bucket, co: str, run_id: str, task: str, status: str, started_at: str, seconds: float,
                outputs: dict | None = None, error: str | None = None) -> dict:
    """The task's entry: succeeded, failed or skipped. outputs' model_cost_usd and workbook are also set at the top."""
    rec = load(bucket, co, run_id) or {**blank(co, run_id), "status": "running", "started_at": started_at}
    entry = {"status": status, "started_at": started_at, "ended_at": now(), "seconds": round(seconds, 1),
             "outputs": outputs or {}}
    if error:
        entry["error"] = error
    rec["tasks"][task] = entry
    if "model_cost_usd" in entry["outputs"]:
        rec["model_cost_usd"][task] = entry["outputs"]["model_cost_usd"]
    if entry["outputs"].get("workbook"):
        rec["workbook"] = entry["outputs"]["workbook"]
    save(bucket, rec)
    return rec


def finalize(bucket, co: str, run_id: str) -> tuple[dict, list[str]]:
    """Overall status: succeeded when tasks 1-7 all succeeded or were skipped, else failed. Returns the record and the
    tasks that did not succeed (failed, still running, or never ran)."""
    rec = load(bucket, co, run_id) or blank(co, run_id)
    bad = [t for t in PIPELINE if (rec["tasks"].get(t) or {}).get("status") not in DONE]
    rec.update(status="failed" if bad else "succeeded", ended_at=now())
    save(bucket, rec)
    return rec, bad
