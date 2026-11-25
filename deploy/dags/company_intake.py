"""
### company_intake

Every 15 minutes: start a **company_evidence** run for each company whose input file `input/<co>/company.json`
is new or has changed since its last run, or whose last run ended more than `px_refresh_days` ago; never while a
run of that company is still active (deploy/INTERFACES.md, section DAGs; the rules are in `px_intake.py`).

- `plan_runs` lists the inputs (with ETag) and each company's latest run record in `s3://<px_bucket>/runs/<co>/`,
  asks Airflow whether a record that looks stale (`queued`/`running` for more than `px_stale_hours`) is still
  queued or running, and decides. It writes nothing, and plans nothing while company_evidence is paused. When
  Airflow cannot answer either question, it fails (and is retried) rather than guess: a guess could start a second
  run of a company that is still running, or queue runs into a paused DAG.
- `start_run`, once per planned run: `queue_run` writes the `queued` record `runs/<co>/<run_id>.json` (only when it
  does not exist: cleared and run again, it leaves the record of a run that has started as it is), then `trigger`
  starts company_evidence with `conf={"company": co, "run_id": run_id}` and the same run id (skipped when that run
  exists).

A failed DAG run e-mails `px_alert_emails` (the DAG's own callback, which the DAG processor runs however a task
failed: a failed trigger call crashes the task runner of Airflow 3.2.2 before a task callback could run). A run whose
trigger never succeeded stays `queued` and is replaced once it is older than `px_stale_hours`.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)

import px_intake  # noqa: E402
import px_settings  # noqa: E402
from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator  # noqa: E402
from airflow.sdk import dag, get_current_context, task, task_group  # noqa: E402

log = logging.getLogger(__name__)

DAG_ID = "company_intake"
EVIDENCE_DAG_ID = "company_evidence"
_WARN_REASONS = {px_intake.INVALID_COMPANY, px_intake.UNREADABLE_RECORD, px_intake.UNKNOWN_STATUS}


def _s3_client(region: str) -> Any:
    from airflow.providers.amazon.aws.hooks.s3 import S3Hook

    return S3Hook(aws_conn_id=px_settings.AWS_CONN_ID, region_name=region).get_conn()


def _evidence_paused(ti: Any) -> bool:
    """Errors are not caught: plan_runs fails and is retried instead of queueing runs into a paused DAG."""
    return bool(ti.get_dag(EVIDENCE_DAG_ID).is_paused)


def _airflow_run_active(ti: Any, run_id: Any) -> bool:
    """Errors are not caught: judging a live run by its record's age alone would start a second run of the company."""
    if not run_id:
        return False
    return ti.get_dr_count(dag_id=EVIDENCE_DAG_ID, run_ids=[str(run_id)], states=["queued", "running"]) > 0


@dag(
    dag_id=DAG_ID,
    schedule="*/15 * * * *",
    start_date=datetime(2026, 11, 1, tzinfo=timezone.utc),
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(hours=1),
    on_failure_callback=px_settings.alert_on_run_failure,
    default_args={
        "retries": 1,
        "retry_delay": timedelta(minutes=1),
    },
    tags=["px", "company-evidence"],
    doc_md=__doc__,
)
def company_intake():
    @task
    def plan_runs() -> list[dict[str, Any]]:
        """Decide which companies get a run; returns one plan (``Decision.to_plan()``) per run to start."""
        bucket, region = px_settings.get("px_bucket"), px_settings.get("px_region")
        refresh_days = px_settings.get_positive_number("px_refresh_days")
        stale_hours = px_settings.get_positive_number("px_stale_hours")
        ti = get_current_context()["ti"]
        if _evidence_paused(ti):
            log.warning("%s is paused: no run is planned", EVIDENCE_DAG_ID)
            return []

        s3 = _s3_client(region)
        inputs = px_intake.list_inputs(s3, bucket)
        records = px_intake.read_latest_records(s3, bucket, inputs)
        now = datetime.now(timezone.utc)
        alive = [
            record["run_id"]
            for record in records.values()
            if px_intake.is_stale_candidate(record, now, stale_hours) and _airflow_run_active(ti, record.get("run_id"))
        ]
        decisions = px_intake.decide(inputs, records, now, refresh_days, stale_hours, alive_run_ids=alive)
        for d in decisions:
            level = logging.WARNING if d.reason in _WARN_REASONS else logging.INFO
            action = f"start {d.run_id}" if d.start else "skip"
            log.log(level, "%s: %s (%s: %s)", d.company, action, d.reason, d.detail)
        plans = [d.to_plan() for d in decisions if d.start]
        log.info("s3://%s/input/: %d compan(ies), %d run(s) to start", bucket, len(inputs), len(plans))
        return plans

    @task(multiple_outputs=True, retries=2, map_index_template="{{ px_company }}")
    def queue_run(plan: dict[str, Any]) -> dict[str, Any]:
        """Write the run's ``queued`` record unless it exists; return the trigger's run id and conf."""
        get_current_context()["px_company"] = plan["company"]
        bucket = px_settings.get("px_bucket")
        record = px_intake.queued_record(plan)
        key = px_intake.record_key(record["company"], record["run_id"])
        if px_intake.create_record(_s3_client(px_settings.get("px_region")), bucket, record):
            log.info("%s: queued s3://%s/%s (%s: %s)", plan["company"], bucket, key, plan["reason"], plan["detail"])
        else:
            # this task was cleared and runs again: the run may have started, so its record is left as it is
            log.warning("%s: s3://%s/%s exists already; left as it is", plan["company"], bucket, key)
        return {"run_id": plan["run_id"], "conf": {"company": plan["company"], "run_id": plan["run_id"]}}

    @task_group(group_id="start_run")
    def start_run(plan: dict[str, Any]) -> None:
        queued = queue_run(plan)
        TriggerDagRunOperator(
            task_id="trigger",
            trigger_dag_id=EVIDENCE_DAG_ID,
            trigger_run_id=queued["run_id"],
            conf=queued["conf"],
            logical_date=None,
            wait_for_completion=False,
            skip_when_already_exists=True,
            openlineage_inject_parent_info=False,
            retries=2,
            map_index_template="{{ task.conf['company'] }}",
        )

    start_run.expand(plan=plan_runs())


company_intake()
