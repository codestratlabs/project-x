"""The intake decision (px_intake.decide) and its S3 helpers. No Airflow; S3 is moto."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import px_intake
import pytest
from px_intake import Decision, UnreadableRecord, decide

UTC = timezone.utc
NOW = datetime(2026, 11, 17, 10, 15, 0, tzinfo=UTC)
ETAG = '"1a2b3c4d5e6f708192a3b4c5d6e7f809"'  # as S3 returns it, quotes included
ETAG_NEW = '"9f8e7d6c5b4a39281706f5e4d3c2b1a0"'
RUN_ID_FORMAT = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")


def z(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def record(status: str, *, etag: str = ETAG, created: datetime, started=None, ended=None, tasks=None, run_id=None):
    run_id = run_id or f"{created.strftime('%Y%m%dT%H%M%SZ')}-{px_intake.normalize_etag(etag)[:8]}"
    return {
        "company": "lungpacer",
        "run_id": run_id,
        "status": status,
        "input_etag": px_intake.normalize_etag(etag),
        "created_at": z(created),
        "started_at": z(started) if started else None,
        "ended_at": z(ended) if ended else None,
        "tasks": tasks or {},
    }


def only(decisions: list[Decision]) -> Decision:
    assert len(decisions) == 1
    return decisions[0]


def run(inputs, records, now=NOW, refresh_days=7, stale_hours=24, alive=()):
    return decide(inputs, records, now, refresh_days, stale_hours, alive_run_ids=alive)


# ---- new input -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("records", [{}, {"lungpacer": None}])
def test_new_input_starts_a_run(records):
    d = only(run({"lungpacer": ETAG}, records))
    assert d.start and d.reason == px_intake.NEW
    assert d.run_id == "20261117T101500Z-1a2b3c4d"
    assert d.input_etag == "1a2b3c4d5e6f708192a3b4c5d6e7f809"
    assert d.created_at == "2026-11-17T10:15:00Z"


def test_records_of_companies_without_input_are_ignored():
    decisions = run({"lungpacer": ETAG}, {"gone": record("succeeded", created=NOW - timedelta(days=30))})
    assert [d.company for d in decisions] == ["lungpacer"]


def test_one_decision_per_input_sorted_by_company():
    decisions = run({"lungpacer": ETAG, "databricks": ETAG_NEW, "horizon3_ai": ETAG}, {})
    assert [d.company for d in decisions] == ["databricks", "horizon3_ai", "lungpacer"]
    assert all(d.start for d in decisions)


# ---- changed ETag --------------------------------------------------------------------------------------------------


def test_changed_etag_starts_a_run():
    latest = record("succeeded", created=NOW - timedelta(days=1), ended=NOW - timedelta(hours=20))
    d = only(run({"lungpacer": ETAG_NEW}, {"lungpacer": latest}))
    assert d.start and d.reason == px_intake.CHANGED
    assert d.run_id == "20261117T101500Z-9f8e7d6c"
    assert d.input_etag == "9f8e7d6c5b4a39281706f5e4d3c2b1a0"


def test_etag_comparison_ignores_quotes_and_case():
    latest = record("succeeded", created=NOW - timedelta(days=1), ended=NOW - timedelta(hours=20))
    latest["input_etag"] = ETAG.upper()  # quoted and upper case in the record
    d = only(run({"lungpacer": ETAG.strip('"')}, {"lungpacer": latest}))
    assert not d.start and d.reason == px_intake.UP_TO_DATE


def test_changed_etag_after_a_failed_run_starts_at_once():
    latest = record("failed", created=NOW - timedelta(hours=3), ended=NOW - timedelta(hours=2))
    d = only(run({"lungpacer": ETAG_NEW}, {"lungpacer": latest}))
    assert d.start and d.reason == px_intake.CHANGED


# ---- refresh interval ----------------------------------------------------------------------------------------------


def test_recent_success_with_same_input_is_up_to_date():
    latest = record("succeeded", created=NOW - timedelta(days=2), ended=NOW - timedelta(days=2) + timedelta(hours=3))
    d = only(run({"lungpacer": ETAG}, {"lungpacer": latest}))
    assert not d.start and d.reason == px_intake.UP_TO_DATE and d.run_id is None


def test_success_older_than_refresh_interval_starts_a_run():
    ended = NOW - timedelta(days=7, seconds=1)
    latest = record("succeeded", created=ended - timedelta(hours=2), ended=ended)
    d = only(run({"lungpacer": ETAG}, {"lungpacer": latest}))
    assert d.start and d.reason == px_intake.REFRESH


def test_refresh_needs_more_than_the_interval():
    ended = NOW - timedelta(days=7)  # exactly 7 days: not "more than"
    latest = record("succeeded", created=ended - timedelta(hours=2), ended=ended)
    assert not only(run({"lungpacer": ETAG}, {"lungpacer": latest})).start


def test_refresh_interval_comes_from_the_argument():
    ended = NOW - timedelta(days=2)
    latest = record("succeeded", created=ended - timedelta(hours=2), ended=ended)
    assert only(run({"lungpacer": ETAG}, {"lungpacer": latest}, refresh_days=1)).reason == px_intake.REFRESH
    assert only(run({"lungpacer": ETAG}, {"lungpacer": latest}, refresh_days=3)).reason == px_intake.UP_TO_DATE


def test_recently_failed_run_is_not_restarted():
    latest = record("failed", created=NOW - timedelta(hours=3), ended=NOW - timedelta(hours=2))
    d = only(run({"lungpacer": ETAG}, {"lungpacer": latest}))
    assert not d.start and d.reason == px_intake.FAILED_RECENTLY


def test_failed_run_is_retried_after_the_refresh_interval():
    ended = NOW - timedelta(days=8)
    latest = record("failed", created=ended - timedelta(hours=1), ended=ended)
    d = only(run({"lungpacer": ETAG}, {"lungpacer": latest}))
    assert d.start and d.reason == px_intake.REFRESH


# ---- active run ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "latest",
    [
        record("queued", created=NOW - timedelta(minutes=5)),
        record("running", created=NOW - timedelta(hours=23), started=NOW - timedelta(hours=23)),
    ],
    ids=["queued", "running"],
)
def test_active_run_blocks_a_new_run_even_when_the_input_changed(latest):
    for etag in (ETAG, ETAG_NEW):
        d = only(run({"lungpacer": etag}, {"lungpacer": latest}))
        assert not d.start and d.reason == px_intake.ACTIVE


def test_active_run_blocks_even_when_its_last_success_is_old():
    latest = record("running", created=NOW - timedelta(hours=2), started=NOW - timedelta(hours=2))
    d = only(run({"lungpacer": ETAG}, {"lungpacer": latest}, refresh_days=0.01))
    assert not d.start and d.reason == px_intake.ACTIVE


def test_a_long_run_that_keeps_finishing_tasks_stays_active():
    started = NOW - timedelta(hours=30)
    tasks = {
        "aliases": {"status": "succeeded", "started_at": z(started), "ended_at": z(started + timedelta(minutes=1))},
        "evidence": {"status": "running", "started_at": z(NOW - timedelta(hours=2))},
    }
    latest = record("running", created=started, started=started, tasks=tasks)
    assert only(run({"lungpacer": ETAG}, {"lungpacer": latest})).reason == px_intake.ACTIVE


# ---- stale active run ----------------------------------------------------------------------------------------------


def test_stale_queued_run_is_replaced():
    latest = record("queued", created=NOW - timedelta(hours=25))
    d = only(run({"lungpacer": ETAG}, {"lungpacer": latest}))
    assert d.start and d.reason == px_intake.STALE
    assert d.run_id != latest["run_id"]


def test_stale_running_run_is_replaced():
    started = NOW - timedelta(hours=26)
    tasks = {"evidence": {"status": "running", "started_at": z(NOW - timedelta(hours=24, minutes=1))}}
    latest = record("running", created=started, started=started, tasks=tasks)
    d = only(run({"lungpacer": ETAG}, {"lungpacer": latest}))
    assert d.start and d.reason == px_intake.STALE


def test_stale_run_with_changed_input_starts_as_changed():
    latest = record("running", created=NOW - timedelta(days=2), started=NOW - timedelta(days=2))
    d = only(run({"lungpacer": ETAG_NEW}, {"lungpacer": latest}))
    assert d.start and d.reason == px_intake.CHANGED


def test_stale_hours_come_from_the_argument():
    latest = record("queued", created=NOW - timedelta(hours=5))
    assert only(run({"lungpacer": ETAG}, {"lungpacer": latest}, stale_hours=4)).reason == px_intake.STALE
    assert only(run({"lungpacer": ETAG}, {"lungpacer": latest}, stale_hours=6)).reason == px_intake.ACTIVE


def test_old_record_that_airflow_reports_active_still_blocks():
    latest = record("queued", created=NOW - timedelta(hours=40))
    d = only(run({"lungpacer": ETAG_NEW}, {"lungpacer": latest}, alive=[latest["run_id"]]))
    assert not d.start and d.reason == px_intake.ACTIVE


def test_is_stale_candidate():
    young = record("queued", created=NOW - timedelta(hours=1))
    old = record("running", created=NOW - timedelta(hours=30), started=NOW - timedelta(hours=30))
    done = record("succeeded", created=NOW - timedelta(days=30), ended=NOW - timedelta(days=30))
    assert not px_intake.is_stale_candidate(young, NOW, 24)
    assert px_intake.is_stale_candidate(old, NOW, 24)
    assert not px_intake.is_stale_candidate(done, NOW, 24)
    assert not px_intake.is_stale_candidate(None, NOW, 24)
    assert not px_intake.is_stale_candidate(UnreadableRecord("x", "bad"), NOW, 24)


# ---- run_id format -------------------------------------------------------------------------------------------------


def test_run_id_format():
    d = only(run({"lungpacer": ETAG}, {}))
    assert RUN_ID_FORMAT.match(d.run_id)
    when, suffix = d.run_id.split("-")
    assert datetime.strptime(when, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC) == NOW
    assert suffix == ETAG.strip('"')[:8]
    assert d.created_at == "2026-11-17T10:15:00Z"
    assert px_intake.RUN_ID_RE.match(d.run_id) and re.match(r"^[A-Za-z0-9_.~:+-]+$", d.run_id)


def test_run_id_uses_utc_time():
    india = timezone(timedelta(hours=5, minutes=30))
    d = only(run({"lungpacer": ETAG}, {}, now=datetime(2026, 11, 17, 15, 45, 0, tzinfo=india)))
    assert d.run_id == "20261117T101500Z-1a2b3c4d"


def test_run_id_from_multipart_etag():
    assert px_intake.make_run_id(NOW, '"abcdef0123456789abcdef0123456789-3"') == "20261117T101500Z-abcdef01"


def test_run_ids_are_unique_within_one_call():
    decisions = run({"a": ETAG, "b": ETAG, "c": ETAG}, {})
    ids = [d.run_id for d in decisions]
    assert ids == ["20261117T101500Z-1a2b3c4d", "20261117T101501Z-1a2b3c4d", "20261117T101502Z-1a2b3c4d"]
    assert [d.created_at for d in decisions] == ["2026-11-17T10:15:00Z", "2026-11-17T10:15:01Z", "2026-11-17T10:15:02Z"]


# ---- records the intake cannot use ---------------------------------------------------------------------------------


def test_invalid_company_folder_is_skipped():
    names = ["bad name", "-x", "acme.io", "a" * 101, "acme\n", "ok", "horizon3_ai", "a" * 100]
    by_company = {d.company: d for d in run(dict.fromkeys(names, ETAG), {})}
    for bad in names[:5]:
        assert by_company[bad].reason == px_intake.INVALID_COMPANY and not by_company[bad].start, bad
    assert "'_' or '-'" in by_company["acme.io"].detail and "'.'" not in by_company["acme.io"].detail
    assert all(by_company[ok].start for ok in names[5:])


def test_run_id_pattern():
    assert px_intake.RUN_ID_RE.fullmatch("20261117T101500Z-1a2b3c4d") and px_intake.RUN_ID_RE.fullmatch("a.b" + "c" * 197)
    for bad in ("a..b", "x" * 201, ".x", "manual__2026-11-17T16:26:21+00:00", "a/b"):
        assert not px_intake.RUN_ID_RE.fullmatch(bad), bad


def test_names_the_intake_accepts_are_names_the_runner_accepts(monkeypatch):
    """Every company and run id the intake (and the company_evidence Params) let through gets past the runner's
    argument check: otherwise every task of the run, finalize too, would stop before writing the record."""
    pytest.importorskip("boto3", reason="the runner needs boto3")
    import signal
    import sys
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "runner"))
    from px_runner import __main__ as runner_main
    from px_runner import tasks

    monkeypatch.setattr(tasks, "client", lambda service, settings: None)
    monkeypatch.setattr(tasks, "run", lambda *args: 0)
    previous = signal.getsignal(signal.SIGTERM)

    def runner_accepts(company, run_id):
        try:
            return runner_main.main(["finalize", "--company", company, "--run-id", run_id]) == 0
        except SystemExit:
            return False
        finally:
            signal.signal(signal.SIGTERM, previous)

    run_id = "20261117T101500Z-1a2b3c4d"
    companies = ["lungpacer", "horizon3_ai", "x-1", "a" * 100, "a" * 101, "acme.io", "-x", "a..b", "bad name"]
    for co in companies:
        assert bool(px_intake.COMPANY_RE.fullmatch(co)) == runner_accepts(co, run_id), co
    for rid in [run_id, "a.b", "a..b", "x" * 200, "x" * 201, ".x", "x_y-z.1"]:
        if px_intake.RUN_ID_RE.fullmatch(rid):
            assert runner_accepts("lungpacer", rid), rid
    assert "px_runner" in sys.modules


def test_running_record_whose_tasks_all_finished_counts_as_succeeded():
    """finalize failed after publish: the workbook is out, so the run is not started again as a dead run."""
    started = NOW - timedelta(hours=30)
    tasks = {t: {"status": "skipped" if t == "download" else "succeeded", "started_at": z(started),
                 "ended_at": z(started + timedelta(hours=1))} for t in px_intake.PIPELINE_TASKS}
    latest = record("running", created=started, started=started, tasks=tasks)
    d = only(run({"lungpacer": ETAG}, {"lungpacer": latest}))
    assert not d.start and d.reason == px_intake.UP_TO_DATE and "clear finalize" in d.detail
    later = started + timedelta(hours=1, days=7, minutes=1)
    assert only(run({"lungpacer": ETAG}, {"lungpacer": latest}, now=later)).reason == px_intake.REFRESH
    assert only(run({"lungpacer": ETAG_NEW}, {"lungpacer": latest})).reason == px_intake.CHANGED
    assert only(run({"lungpacer": ETAG}, {"lungpacer": latest}, alive=[latest["run_id"]])).reason == px_intake.ACTIVE
    tasks["publish"]["status"] = "failed"  # a task did not finish: a dead run, started again
    assert only(run({"lungpacer": ETAG}, {"lungpacer": latest})).reason == px_intake.STALE


def test_unreadable_record_is_left_alone():
    d = only(run({"lungpacer": ETAG}, {"lungpacer": UnreadableRecord("20261110T000000Z-1a2b3c4d", "bad JSON")}))
    assert not d.start and d.reason == px_intake.UNREADABLE_RECORD


def test_unknown_status_is_left_alone():
    latest = record("cancelled", created=NOW - timedelta(days=30))
    d = only(run({"lungpacer": ETAG_NEW}, {"lungpacer": latest}))
    assert not d.start and d.reason == px_intake.UNKNOWN_STATUS


def test_record_times_with_offsets_and_fractions():
    assert px_intake.parse_time("2026-11-17T10:15:00.123456Z") == datetime(2026, 11, 17, 10, 15, 0, 123456, UTC)
    assert px_intake.parse_time("2026-11-17T15:45:00+05:30") == NOW
    assert px_intake.parse_time("2026-11-17T10:15:00") == NOW  # naive: UTC
    assert px_intake.parse_time("yesterday") is None and px_intake.parse_time(None) is None


def test_record_without_times_falls_back_to_its_run_id_time():
    latest = {"run_id": "20261117T080000Z-1a2b3c4d", "status": "queued", "input_etag": ETAG.strip('"')}
    assert px_intake.last_activity(latest) == datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
    assert only(run({"lungpacer": ETAG}, {"lungpacer": latest})).reason == px_intake.ACTIVE


# ---- record helpers ------------------------------------------------------------------------------------------------


def test_queued_record_is_the_contract_shape():
    plan = only(run({"lungpacer": ETAG}, {})).to_plan()
    rec = px_intake.queued_record(plan)
    assert rec == {
        "company": "lungpacer",
        "run_id": "20261117T101500Z-1a2b3c4d",
        "status": "queued",
        "input_etag": "1a2b3c4d5e6f708192a3b4c5d6e7f809",
        "created_at": "2026-11-17T10:15:00Z",
        "tasks": {},
    }
    json.dumps(plan)  # the plan travels as an XCom


def test_latest_run_id_prefers_the_time_in_the_run_id():
    old_but_rewritten = ("20261101T000000Z-aaaaaaaa", datetime(2026, 11, 17, tzinfo=UTC))
    newest = ("20261110T000000Z-bbbbbbbb", datetime(2026, 11, 10, tzinfo=UTC))
    assert px_intake.latest_run_id([old_but_rewritten, newest]) == "20261110T000000Z-bbbbbbbb"
    manual = ("manual-rerun", datetime(2026, 11, 12, tzinfo=UTC))
    assert px_intake.latest_run_id([newest, manual]) == "manual-rerun"
    assert px_intake.latest_run_id([]) is None


# ---- S3 helpers on moto --------------------------------------------------------------------------------------------


@pytest.fixture
def s3(fake_aws):
    moto = pytest.importorskip("moto", reason="moto is not installed")
    import boto3

    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-2")
        client.create_bucket(Bucket="test-bucket", CreateBucketConfiguration={"LocationConstraint": "us-east-2"})
        yield client


def put(s3, key, body):
    s3.put_object(Bucket="test-bucket", Key=key, Body=body.encode() if isinstance(body, str) else body)
    return px_intake.normalize_etag(s3.head_object(Bucket="test-bucket", Key=key)["ETag"])


def test_list_inputs(s3):
    lungpacer = put(s3, "input/lungpacer/company.json", '{"name": "Lungpacer"}')
    horizon = put(s3, "input/horizon3_ai/company.json", '{"name": "Horizon3"}')
    put(s3, "input/lungpacer/notes.txt", "not an input")
    put(s3, "input/nested/deeper/company.json", "{}")
    put(s3, "input/company.json", "{}")
    put(s3, "raw/lungpacer/sec/manifest.csv", "x")
    assert px_intake.list_inputs(s3, "test-bucket") == {"lungpacer": lungpacer, "horizon3_ai": horizon}
    assert re.fullmatch(r"[0-9a-f]{32}", lungpacer)


def test_read_latest_records(s3):
    older = record("succeeded", created=NOW - timedelta(days=9), ended=NOW - timedelta(days=9))
    newer = record("failed", created=NOW - timedelta(days=1), ended=NOW - timedelta(days=1))
    put(s3, px_intake.record_key("lungpacer", newer["run_id"]), json.dumps(newer))
    put(s3, px_intake.record_key("lungpacer", older["run_id"]), json.dumps(older))  # written last
    put(s3, "runs/lungpacer/sub/20991231T000000Z-ffffffff.json", "{}")  # not a record of this company
    put(s3, "runs/broken/20261117T000000Z-1a2b3c4d.json", "{not json")
    no_run_id = {k: v for k, v in older.items() if k != "run_id"}
    put(s3, "runs/norunid/20261101T000000Z-1a2b3c4d.json", json.dumps(no_run_id))

    companies = ["lungpacer", "horizon3_ai", "broken", "norunid", "bad name"]
    found = px_intake.read_latest_records(s3, "test-bucket", companies)
    assert found["lungpacer"] == newer
    assert found["horizon3_ai"] is None
    assert isinstance(found["broken"], UnreadableRecord) and found["broken"].run_id == "20261117T000000Z-1a2b3c4d"
    assert found["norunid"]["run_id"] == "20261101T000000Z-1a2b3c4d"
    assert "bad name" not in found


def test_put_record_writes_json_at_the_contract_key(s3):
    plan = only(run({"lungpacer": ETAG}, {})).to_plan()
    key = px_intake.put_record(s3, "test-bucket", px_intake.queued_record(plan))
    assert key == "runs/lungpacer/20261117T101500Z-1a2b3c4d.json"
    obj = s3.get_object(Bucket="test-bucket", Key=key)
    assert obj["ContentType"] == "application/json"
    assert json.loads(obj["Body"].read())["status"] == "queued"


def test_create_record_never_replaces_a_record(s3):
    """The intake's queued record is a conditional write: run again (a cleared queue_run), it leaves the record of a
    run that has started as it is."""
    queued = px_intake.queued_record(only(run({"lungpacer": ETAG}, {})).to_plan())
    key = px_intake.record_key("lungpacer", queued["run_id"])
    assert px_intake.create_record(s3, "test-bucket", queued) is True
    obj = s3.get_object(Bucket="test-bucket", Key=key)
    assert obj["ContentType"] == "application/json" and json.loads(obj["Body"].read()) == queued
    started = {**queued, "status": "running", "tasks": {"aliases": {"status": "succeeded"}}}
    px_intake.put_record(s3, "test-bucket", started)  # the runner's first task
    assert px_intake.create_record(s3, "test-bucket", queued) is False
    assert json.loads(s3.get_object(Bucket="test-bucket", Key=key)["Body"].read()) == started


def test_create_record_raises_other_s3_errors(s3):
    queued = px_intake.queued_record(only(run({"lungpacer": ETAG}, {})).to_plan())
    with pytest.raises(Exception, match="NoSuchBucket"):
        px_intake.create_record(s3, "no-such-bucket", queued)


def test_intake_round_trip_on_s3(s3):
    """List, read, decide and queue as the DAG does; the next pass sees the queued run and starts nothing."""
    put(s3, "input/lungpacer/company.json", '{"name": "Lungpacer"}')
    inputs = px_intake.list_inputs(s3, "test-bucket")
    first = run(inputs, px_intake.read_latest_records(s3, "test-bucket", inputs))
    assert [d.reason for d in first] == [px_intake.NEW]
    px_intake.put_record(s3, "test-bucket", px_intake.queued_record(first[0].to_plan()))

    later = NOW + timedelta(minutes=15)
    second = run(inputs, px_intake.read_latest_records(s3, "test-bucket", inputs), now=later)
    assert [(d.start, d.reason) for d in second] == [(False, px_intake.ACTIVE)]

    put(s3, "input/lungpacer/company.json", '{"name": "Lungpacer Medical"}')  # edited while queued
    inputs = px_intake.list_inputs(s3, "test-bucket")
    stale_by_then = later + timedelta(days=1, minutes=1)  # the queued record is older than px_stale_hours
    third = run(inputs, px_intake.read_latest_records(s3, "test-bucket", inputs), now=stale_by_then)
    assert [(d.start, d.reason) for d in third] == [(True, px_intake.CHANGED)]
