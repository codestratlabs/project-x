"""Intake decision for the company_intake DAG: which companies get a new company_evidence run.

Standard library only, so it is tested without Airflow (deploy/dags/tests/test_px_intake.py):

- ``decide()`` is the decision itself: a pure function, no I/O.
- The S3 helpers at the bottom take a boto3 S3 client; the DAG passes the client of its S3Hook, the tests a moto one.

Rules (deploy/INTERFACES.md, section DAGs), for each ``input/<co>/company.json``:

1. Never start a run while the company's latest record is ``queued`` or ``running`` and younger than
   ``px_stale_hours`` ("active"). A record's age is the time since its last change (the latest of ``created_at``,
   ``started_at``, ``ended_at`` and the task times), so a long run that keeps finishing tasks stays active. A run that
   Airflow still reports as queued or running (``alive_run_ids``) is active at any age, so a run waiting behind
   ``max_active_runs`` is never doubled.
2. Start when the company has no run record ("new").
3. Start when the input ETag differs from the latest record's ``input_etag`` ("changed").
4. Start when the latest successful run ended more than ``px_refresh_days`` ago ("refresh"). Only the latest record
   is read, so a failed latest run counts for this rule only once it ended more than ``px_refresh_days`` ago (the
   last success, if any, is older still). A failing company is therefore retried once per refresh interval, never
   every 15 minutes.
5. A ``queued`` or ``running`` record older than ``px_stale_hours`` (and not alive in Airflow) is a run that died
   before ``finalize``; its reason to run still holds, so a new run starts ("stale"). The exception is a ``running``
   record whose tasks 1-7 all succeeded or were skipped: the workbook is published and only ``finalize`` failed (it
   sent its own e-mail), so the run counts as succeeded for rules 3 and 4.

A record that cannot be read or has a status outside the contract is left alone (skip, logged by the DAG): the
intake cannot tell whether that run is active.

``run_id`` = ``<UTC time YYYYMMDDTHHMMSSZ>-<first 8 characters of the input ETag>``. Two runs planned in one call
never share a run id (Airflow run ids are unique per DAG): a clash moves the later one's time on by one second.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# A company key is the folder name under input/ and becomes part of S3 keys, local paths and container names. Both
# patterns are the task runner's own argument checks (deploy/runner/px_runner/__main__.py), so the intake never starts
# a run whose every task the runner would refuse; the run id pattern is narrower (no ':' or '+'). They are also the
# company_evidence Params' patterns (JSON Schema: no \Z, so callers here use fullmatch).
COMPANY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
# A run id becomes part of S3 keys and is the Airflow run id of company_evidence.
RUN_ID_RE = re.compile(r"^(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")
RUN_ID_TIME_FORMAT = "%Y%m%dT%H%M%SZ"
_RUN_ID_TIME_RE = re.compile(r"^(\d{8}T\d{6}Z)")
_INPUT_KEY_RE = re.compile(r"^input/([^/]+)/company\.json$")

QUEUED, RUNNING, SUCCEEDED, FAILED = "queued", "running", "succeeded", "failed"
ACTIVE_STATUSES = frozenset({QUEUED, RUNNING})
KNOWN_STATUSES = frozenset({QUEUED, RUNNING, SUCCEEDED, FAILED})
# Tasks 1-7 of company_evidence (finalize is the eighth) and the task statuses that let a run succeed.
PIPELINE_TASKS = ("aliases", "download", "finder", "records", "evidence", "indirect", "publish")
TASK_DONE = frozenset({"succeeded", "skipped"})

# Decision reasons.
NEW, CHANGED, REFRESH, STALE = "new", "changed", "refresh", "stale"
ACTIVE, UP_TO_DATE, FAILED_RECENTLY = "active", "up_to_date", "failed_recently"
INVALID_COMPANY, UNREADABLE_RECORD, UNKNOWN_STATUS = "invalid_company", "unreadable_record", "unknown_status"


@dataclass(frozen=True)
class Decision:
    """What the intake does for one company. ``run_id``, ``input_etag`` and ``created_at`` are set when it starts."""

    company: str
    start: bool
    reason: str
    detail: str = ""
    run_id: str | None = None
    input_etag: str | None = None
    created_at: str | None = None

    def to_plan(self) -> dict[str, Any]:
        """The decision as a JSON-serialisable dict (the XCom the DAG passes on)."""
        return asdict(self)


@dataclass(frozen=True)
class UnreadableRecord:
    """Stands in for a latest run record whose JSON could not be parsed."""

    run_id: str
    error: str


# ---- small pure helpers ------------------------------------------------------------------------------------------


def normalize_etag(etag: Any) -> str:
    """ETag without quotes or weak prefix, lower case (S3 returns ``"<hex>"`` with the quotes)."""
    text = str(etag or "").strip()
    if text[:2] in ("W/", "w/"):
        text = text[2:]
    return text.strip().strip('"').strip().lower()


def make_run_id(when: datetime, etag: Any) -> str:
    """``<UTC time YYYYMMDDTHHMMSSZ>-<first 8 characters of the ETag>``."""
    suffix = re.sub(r"[^0-9a-z]", "", normalize_etag(etag))[:8] or "noetag"
    return f"{to_utc(when).strftime(RUN_ID_TIME_FORMAT)}-{suffix}"


def to_utc(when: datetime) -> datetime:
    """An aware UTC datetime; a naive one is taken to be UTC already."""
    if when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def iso_z(when: datetime) -> str:
    """UTC ISO 8601 with ``Z``, to the second (the run record's time format)."""
    return to_utc(when).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: Any) -> datetime | None:
    """A run-record time (ISO 8601, ``Z`` or offset) as an aware UTC datetime; None when missing or unreadable."""
    if isinstance(value, datetime):
        return to_utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return to_utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def run_id_time(run_id: Any) -> datetime | None:
    """The time at the start of a run id made by ``make_run_id``; None for other run ids."""
    match = _RUN_ID_TIME_RE.match(str(run_id or ""))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), RUN_ID_TIME_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def last_activity(record: Mapping[str, Any]) -> datetime | None:
    """When a run record last changed: the latest of its own and its tasks' times (else its run id's time)."""
    times = [parse_time(record.get(field)) for field in ("created_at", "started_at", "ended_at")]
    tasks = record.get("tasks")
    if isinstance(tasks, Mapping):
        for entry in tasks.values():
            if isinstance(entry, Mapping):
                times += [parse_time(entry.get("started_at")), parse_time(entry.get("ended_at"))]
    known = [t for t in times if t is not None]
    if known:
        return max(known)
    return run_id_time(record.get("run_id"))


def pipeline_done(record: Mapping[str, Any]) -> bool:
    """True when every one of tasks 1-7 has a succeeded or skipped entry in the record."""
    tasks = record.get("tasks")
    if not isinstance(tasks, Mapping):
        return False
    return all(isinstance(e := tasks.get(t), Mapping) and e.get("status") in TASK_DONE for t in PIPELINE_TASKS)


def is_stale_candidate(record: Any, now: datetime, stale_hours: float) -> bool:
    """True for a queued/running record old enough to be stale: the DAG asks Airflow whether that run is alive."""
    if not isinstance(record, Mapping) or record.get("status") not in ACTIVE_STATUSES:
        return False
    activity = last_activity(record)
    return activity is None or to_utc(now) - activity >= timedelta(hours=float(stale_hours))


def _age(now: datetime, then: datetime) -> str:
    seconds = max(0, int((now - then).total_seconds()))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    return f"{days}d {hours}h {rest // 60}m" if days else f"{hours}h {rest // 60}m"


# ---- the decision ------------------------------------------------------------------------------------------------


def decide(
    inputs: Mapping[str, Any],
    latest_records: Mapping[str, Any],
    now: datetime,
    refresh_days: float,
    stale_hours: float,
    alive_run_ids: Collection[str] = (),
) -> list[Decision]:
    """One decision per input company, sorted by company (see the module docstring for the rules).

    :param inputs: company -> ETag of ``input/<co>/company.json`` (quoted or not).
    :param latest_records: company -> its latest run record (dict), None (no run), or an ``UnreadableRecord``.
        A company missing here has no run.
    :param now: the planning time; it also becomes ``created_at`` and the time in ``run_id``.
    :param refresh_days: ``px_refresh_days``.
    :param stale_hours: ``px_stale_hours``.
    :param alive_run_ids: run ids that Airflow reports as queued or running in company_evidence.
    """
    now = to_utc(now)
    refresh = timedelta(days=float(refresh_days))
    stale = timedelta(hours=float(stale_hours))
    alive = set(alive_run_ids)
    taken: set[str] = set()
    decisions: list[Decision] = []

    def start(company: str, etag: str, reason: str, detail: str) -> Decision:
        planned = now
        run_id = make_run_id(planned, etag)
        while run_id in taken:
            planned += timedelta(seconds=1)
            run_id = make_run_id(planned, etag)
        taken.add(run_id)
        return Decision(company, True, reason, detail, run_id, etag, iso_z(planned))

    for company in sorted(inputs):
        etag = normalize_etag(inputs[company])
        if not COMPANY_RE.fullmatch(company):
            decisions.append(
                Decision(
                    company,
                    False,
                    INVALID_COMPANY,
                    "name the folder with letters, digits, '_' or '-' (first a letter or digit, at most 100)",
                )
            )
            continue

        record = latest_records.get(company)
        if record is None:
            decisions.append(start(company, etag, NEW, "no run record"))
            continue
        if isinstance(record, UnreadableRecord):
            decisions.append(
                Decision(company, False, UNREADABLE_RECORD, f"run {record.run_id}: {record.error}")
            )
            continue
        if not isinstance(record, Mapping):
            decisions.append(Decision(company, False, UNREADABLE_RECORD, f"not a JSON object: {type(record)}"))
            continue

        status = record.get("status")
        run_id = record.get("run_id")
        if status not in KNOWN_STATUSES:
            decisions.append(Decision(company, False, UNKNOWN_STATUS, f"run {run_id}: status {status!r}"))
            continue

        activity = last_activity(record)
        if status in ACTIVE_STATUSES:
            if run_id in alive:
                decisions.append(
                    Decision(company, False, ACTIVE, f"run {run_id} is {status}; Airflow reports it still active")
                )
                continue
            if activity is not None and now - activity < stale:
                decisions.append(
                    Decision(company, False, ACTIVE, f"run {run_id} is {status}, last change {_age(now, activity)} ago")
                )
                continue
        note = ""
        if status == RUNNING and pipeline_done(record):
            # tasks 1-7 finished and the workbook is published, but finalize never recorded it (finalize failed and
            # e-mailed): the run counts as succeeded rather than as a dead run to start again
            status, note = SUCCEEDED, "; its finalize did not record this (clear finalize in Airflow)"

        if normalize_etag(record.get("input_etag")) != etag:
            decisions.append(start(company, etag, CHANGED, f"input ETag differs from run {run_id}"))
        elif status in (SUCCEEDED, FAILED):
            ended = parse_time(record.get("ended_at")) or activity
            if ended is None or now - ended > refresh:
                ago = "an unknown time" if ended is None else _age(now, ended)
                decisions.append(start(company, etag, REFRESH, f"run {run_id} {status}, ended {ago} ago{note}"))
            elif status == SUCCEEDED:
                decisions.append(
                    Decision(company, False, UP_TO_DATE, f"run {run_id} succeeded {_age(now, ended)} ago{note}")
                )
            else:
                decisions.append(
                    Decision(
                        company,
                        False,
                        FAILED_RECENTLY,
                        f"run {run_id} failed {_age(now, ended)} ago; a new run starts when the input changes "
                        f"or {float(refresh_days):g} days after it ended",
                    )
                )
        else:
            ago = "an unknown time" if activity is None else _age(now, activity)
            decisions.append(start(company, etag, STALE, f"run {run_id} still {status}, last change {ago} ago"))
    return decisions


def queued_record(plan: Mapping[str, Any]) -> dict[str, Any]:
    """The ``queued`` run record the intake writes before it triggers the run (``plan`` from ``Decision.to_plan``)."""
    return {
        "company": plan["company"],
        "run_id": plan["run_id"],
        "status": QUEUED,
        "input_etag": plan["input_etag"],
        "created_at": plan["created_at"],
        "tasks": {},
    }


# ---- S3 helpers (boto3 client passed in) -------------------------------------------------------------------------


def record_key(company: str, run_id: str) -> str:
    return f"runs/{company}/{run_id}.json"


def list_inputs(s3: Any, bucket: str) -> dict[str, str]:
    """company -> ETag of every ``input/<co>/company.json`` (deeper keys and other files are ignored)."""
    found: dict[str, str] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="input/"):
        for obj in page.get("Contents", []):
            match = _INPUT_KEY_RE.match(obj["Key"])
            if match:
                found[match.group(1)] = normalize_etag(obj.get("ETag"))
    return found


def latest_run_id(objects: Iterable[tuple[str, datetime | None]]) -> str | None:
    """The latest of ``(run_id, LastModified)`` pairs: by the time in the run id, else by LastModified."""
    best: tuple[datetime, str] | None = None
    floor = datetime.min.replace(tzinfo=timezone.utc)
    for run_id, modified in objects:
        when = run_id_time(run_id) or (to_utc(modified) if isinstance(modified, datetime) else floor)
        if best is None or (when, run_id) > best:
            best = (when, run_id)
    return None if best is None else best[1]


def read_latest_records(s3: Any, bucket: str, companies: Iterable[str]) -> dict[str, Any]:
    """company -> latest run record (dict), None (no record), or ``UnreadableRecord`` (JSON that cannot be used).

    S3 errors are not caught: the task fails and Airflow retries it.
    """
    out: dict[str, Any] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for company in companies:
        if not COMPANY_RE.fullmatch(company):
            continue
        prefix = f"runs/{company}/"
        objects: list[tuple[str, datetime | None]] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                name = obj["Key"][len(prefix) :]
                if "/" not in name and name.endswith(".json") and len(name) > len(".json"):
                    objects.append((name[: -len(".json")], obj.get("LastModified")))
        run_id = latest_run_id(objects)
        if run_id is None:
            out[company] = None
            continue
        body = s3.get_object(Bucket=bucket, Key=record_key(company, run_id))["Body"].read()
        try:
            record = json.loads(body)
            if not isinstance(record, dict):
                raise ValueError("the record is not a JSON object")
        except ValueError as exc:
            out[company] = UnreadableRecord(run_id, str(exc)[:200])
            continue
        record.setdefault("run_id", run_id)
        out[company] = record
    return out


def put_record(s3: Any, bucket: str, record: Mapping[str, Any], **extra: Any) -> str:
    """Write a run record to ``runs/<co>/<run_id>.json``; returns the key. ``extra``: more put_object arguments."""
    key = record_key(record["company"], record["run_id"])
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=(json.dumps(record, indent=2) + "\n").encode("utf-8"),
        ContentType="application/json",
        **extra,
    )
    return key


def create_record(s3: Any, bucket: str, record: Mapping[str, Any]) -> bool:
    """Write a new run record to ``runs/<co>/<run_id>.json``: True when written, False when that record exists already.

    An S3 conditional write (``If-None-Match: *``), so the intake's ``queue_run`` cleared and run again never replaces
    the record of a run that has started: its tasks' entries would be lost, and finalize would count them as not run.
    Any other S3 error is raised.
    """
    try:
        put_record(s3, bucket, record, IfNoneMatch="*")
    except Exception as exc:  # botocore's ClientError; this module needs no botocore import
        response = getattr(exc, "response", None) or {}
        code = str(response.get("Error", {}).get("Code", ""))
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code == "PreconditionFailed" or status == 412:
            return False
        raise
    return True
