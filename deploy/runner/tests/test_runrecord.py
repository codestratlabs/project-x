"""Run record updates: queued -> running -> succeeded / failed / skipped, and finalize."""
import json
import re

from conftest import BUCKET, CO, RUN, put, record

from px_runner import runrecord, s3sync

UTC = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
INPUT = f"input/{CO}/company.json"


def bucket(aws):
    return s3sync.Bucket(aws[0], BUCKET)


def queued(s3):
    """The record as the intake DAG writes it."""
    put(s3, f"runs/{CO}/{RUN}.json", json.dumps({"company": CO, "run_id": RUN, "status": "queued",
                                                  "input_etag": "abc123", "created_at": "2026-11-17T10:15:00Z"}))


def test_queued_to_running_keeps_what_the_intake_dag_wrote(aws):
    s3, _ = aws
    queued(s3)
    runrecord.start_task(bucket(aws), CO, RUN, "aliases", "2026-11-17T10:16:00Z", INPUT)
    rec = record(s3)
    assert rec["status"] == "running" and rec["started_at"] == "2026-11-17T10:16:00Z" and rec["ended_at"] is None
    assert rec["input_etag"] == "abc123" and rec["created_at"] == "2026-11-17T10:15:00Z"
    assert rec["tasks"] == {"aliases": {"status": "running", "started_at": "2026-11-17T10:16:00Z"}}
    assert rec["model_cost_usd"] == {"finder": None, "indirect": None} and rec["workbook"] is None
    runrecord.start_task(bucket(aws), CO, RUN, "download", "2026-11-17T10:20:00Z", INPUT)
    assert record(s3)["started_at"] == "2026-11-17T10:16:00Z"          # set once, by the first task


def test_first_task_creates_a_missing_record_with_the_input_etag(aws):
    s3, _ = aws
    put(s3, INPUT, "{}")
    etag = s3.head_object(Bucket=BUCKET, Key=INPUT)["ETag"].strip('"')
    runrecord.start_task(bucket(aws), CO, RUN, "aliases", runrecord.now(), INPUT)
    rec = record(s3)
    assert (rec["company"], rec["run_id"], rec["status"], rec["input_etag"]) == (CO, RUN, "running", etag)
    assert UTC.match(rec["created_at"]) and UTC.match(rec["started_at"])


def test_finish_task_succeeded_failed_skipped(aws):
    s3, _ = aws
    b = bucket(aws)
    queued(s3)
    for task in ("indirect", "publish", "evidence"):
        runrecord.start_task(b, CO, RUN, task, "2026-11-17T10:16:00Z", INPUT)
    runrecord.finish_task(b, CO, RUN, "indirect", "succeeded", "2026-11-17T10:16:00Z", 12.34,
                          {"filings": 61, "model_cost_usd": 0.3449})
    runrecord.finish_task(b, CO, RUN, "publish", "succeeded", "2026-11-17T10:16:00Z", 3,
                          {"workbook": f"reports/{CO}/{RUN}/Acme_findings.xlsx", "sheets": {"About": 12}})
    runrecord.finish_task(b, CO, RUN, "evidence", "failed", "2026-11-17T10:16:00Z", 1, error="locate.py exited with code 1")
    rec = record(s3)
    ind, pub, ev = rec["tasks"]["indirect"], rec["tasks"]["publish"], rec["tasks"]["evidence"]
    assert ind["status"] == "succeeded" and ind["seconds"] == 12.3 and ind["outputs"]["filings"] == 61
    assert UTC.match(ind["ended_at"]) and ind["started_at"] == "2026-11-17T10:16:00Z" and "error" not in ind
    assert rec["model_cost_usd"] == {"finder": None, "indirect": 0.3449}
    assert pub["status"] == "succeeded" and rec["workbook"] == f"reports/{CO}/{RUN}/Acme_findings.xlsx"
    assert ev["status"] == "failed" and ev["error"] == "locate.py exited with code 1" and ev["outputs"] == {}
    assert rec["status"] == "running"                                   # only finalize sets the run's status
    runrecord.finish_task(b, CO, RUN, "evidence", "skipped", "2026-11-17T10:30:00Z", 0, {"reason": "test"})
    assert "error" not in record(s3)["tasks"]["evidence"]               # a later attempt replaces the entry


def test_finalize_succeeds_only_when_every_task_succeeded_or_was_skipped(aws):
    s3, _ = aws
    b = bucket(aws)
    queued(s3)
    for t in runrecord.PIPELINE:
        runrecord.finish_task(b, CO, RUN, t, "skipped" if t == "indirect" else "succeeded", runrecord.now(), 1)
    rec, bad = runrecord.finalize(b, CO, RUN)
    assert (rec["status"], bad) == ("succeeded", []) and UTC.match(record(s3)["ended_at"])
    assert record(s3)["status"] == "succeeded"

    runrecord.finish_task(b, CO, RUN, "publish", "failed", runrecord.now(), 1, error="boom")
    runrecord.start_task(b, CO, RUN, "evidence", runrecord.now(), INPUT)   # re-run after finalize: running again
    assert record(s3)["status"] == "running" and record(s3)["ended_at"] is None
    rec, bad = runrecord.finalize(b, CO, RUN)
    assert rec["status"] == "failed" and bad == ["evidence", "publish"]  # still running counts as not succeeded


def test_finalize_without_tasks_or_record_is_failed(aws):
    s3, _ = aws
    rec, bad = runrecord.finalize(bucket(aws), CO, RUN)
    assert rec["status"] == "failed" and bad == list(runrecord.PIPELINE)
    assert record(s3)["status"] == "failed"
