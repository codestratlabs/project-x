"""
### company_evidence

One run of the company evidence pipeline for one company (deploy/INTERFACES.md, deploy/ARCHITECTURE.html).

Triggered by **company_intake** with `conf={"company": "<co>", "run_id": "<run_id>"}` and the same Airflow run id.
To run a company by hand, trigger it with those two params and the same run id (deploy/README.md section 12): every
task first checks that the conf's `run_id` is this DAG run's id and fails at once otherwise, because the tasks write
under the conf's id and company_intake finds a run in Airflow by that id. (`airflow dags test` cannot set a run id, so
its runs stop there.)

Tasks, in order: `aliases >> download >> finder >> records >> evidence >> indirect >> publish >> finalize`.
Each runs the pipeline image as `python -m px_runner <task> --company <co> --run-id <run_id>` with the `PX_*`
settings as environment, on the backend named by the Variable `px_task_backend`:

- `docker`: DockerOperator on an Airflow worker of the queue `px_queue` (Airflow's default queue when it is not set;
  the large queue `px_large_queue` for `evidence`). Every container on a worker uses that worker's AWS role. Before a
  task starts its container, it stops and removes the containers that earlier tries of the same task left on that
  Docker daemon (a try whose worker died), and removes the exited containers of the company's earlier runs there. A
  container gets `docker stop` (the runner records the stop) before it is removed, also when its try times out. A try
  that Airflow stops from outside (the DAG run's timeout, the task marked failed) is killed 5 seconds later, before
  it can remove its container: that one stays, exited, until the company's next run on that worker removes it.
- `ecs`: EcsRunTaskOperator (Fargate) with a command override; `evidence` gets the larger task size
  (`px_ecs_large_overrides`). Each task runs as the task role of its secret: `download` as `px_ecs_task_role_sec`,
  `finder` and `indirect` as `px_ecs_task_role_llm`, the others as the task definition's role, which reads no secret.

`download` runs in the SEC pool (`px_sec_pool`); on `docker`, at most `px_large_slots` `evidence` tasks run at once.
Tasks retry twice, 5 minutes apart, and each try is stopped after `px_task_timeout_hours`. `finalize` always runs
(`all_done`) and ends the DAG run `failed` when a pipeline task did not succeed, so the run's state here matches its
run record `runs/<co>/<run_id>.json`; it retries only when it did not work itself (a Docker or ECS error, S3). A task
that fails e-mails `px_alert_emails`; `finalize` e-mails only when it did not work itself (the runner records a failed
run and exits 0). A DAG run that is still not done after `px_run_timeout_hours` (a hung task, a pool that does not
exist) is failed by Airflow and e-mailed by the DAG's own callback.

An invalid Variable (unknown backend, ECS settings missing, malformed JSON) does not stop this file from importing:
every task then fails at once with the reason, and the failure e-mail carries it.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import sys
from collections.abc import Mapping, MutableMapping
from datetime import datetime, timedelta, timezone
from typing import Any

# The helper modules sit next to this file; Airflow puts the DAGs folder on sys.path, a subfolder it does not.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)

import px_intake  # noqa: E402
import px_settings  # noqa: E402
from airflow.sdk import BaseOperator, Param, TriggerRule, chain, dag  # noqa: E402

try:
    from airflow.sdk.exceptions import AirflowFailException
except ImportError:  # pragma: no cover - older Airflow 3
    from airflow.exceptions import AirflowFailException

log = logging.getLogger(__name__)

DAG_ID = "company_evidence"
PIPELINE_TASKS = px_intake.PIPELINE_TASKS          # aliases ... publish
FINALIZE = "finalize"
SEC_TASK = "download"
LARGE_TASK = "evidence"
# The image's entrypoint; set again for the docker backend so a task never depends on how the image was built.
ENTRYPOINT = ["python", "-m", "px_runner"]
_OK_STATES = ("success", "skipped")
# The secret each task reads (deploy/INTERFACES.md, Runner) and so, on ecs, the task role it runs as; other tasks run
# as the task definition's role, which reads no secret.
TASK_ROLE = {"download": "task_role_sec", "finder": "task_role_llm", "indirect": "task_role_llm"}
_ROLE_ARN_RE = re.compile(r"arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]+")
# How long an earlier try's container gets to record its stop (the runner's SIGTERM handler) before Docker kills it.
STOP_SECONDS = 60


def runner_args(task_id: str) -> list[str]:
    """The arguments after ``python -m px_runner`` (templated from the DAG run's params)."""
    return [task_id, "--company", "{{ params.company }}", "--run-id", "{{ params.run_id }}"]


def fail_if_run_failed(context: MutableMapping[str, Any], result: Any = None) -> None:
    """``post_execute`` of ``finalize``: raise ``RunFailed`` when a pipeline task did not succeed.

    ``finalize`` is the DAG's only leaf, and Airflow sets a DAG run's state from its leaves; without this check a
    run whose ``download`` failed would show ``success`` once ``finalize`` had written the record (the runner's
    finalize exits 0 for a failed run too). When the states cannot be read, ``finalize`` fails (it is retried, then
    e-mailed) rather than let a failed run show ``success``; clearing ``finalize`` checks again.
    """
    ti = context["ti"]
    try:
        found = ti.get_task_states(dag_id=ti.dag_id, task_ids=list(PIPELINE_TASKS), run_ids=[ti.run_id])
        states = {task: getattr(state, "value", state) for task, state in found.get(ti.run_id, {}).items()}
    except Exception as exc:
        raise RuntimeError(f"finalize recorded the run but cannot read its task states ({exc}); clear finalize") from exc
    bad = {task: states.get(task) for task in PIPELINE_TASKS if states.get(task) not in _OK_STATES}
    if bad:
        summary = ", ".join(f"{task} {state or 'not run'}" for task, state in bad.items())
        px_settings.raise_run_failed(context, f"run {ti.run_id} failed ({summary}); its record is finalized")


def check_run_id(context: MutableMapping[str, Any]) -> None:
    """``pre_execute`` of every task: the conf's ``run_id`` (used in every S3 key) must be this DAG run's id.

    company_intake asks Airflow about a run by its record's run id, so a run triggered under another id (the UI's
    default ``manual__...``) would look dead to it and be started again, and two DAG runs with the same conf would
    write the same record and files. ``finalize`` raises ``RunFailed`` (no second e-mail) and writes nothing.
    """
    conf_run_id, run_id = (context.get("params") or {}).get("run_id"), context.get("run_id")
    if conf_run_id != run_id:
        message = (
            f"conf run_id {conf_run_id!r} is not this DAG run's id {run_id!r}: trigger company_evidence with the "
            "run id of its conf (deploy/README.md section 12)"
        )
        if context["ti"].task_id == FINALIZE:
            px_settings.raise_run_failed(context, message)
        raise AirflowFailException(message)


def container_prefix(company: str, task_id: str, run_id: str) -> str:
    """The name of a task's container without its try number (``_docker_task``'s ``container_name``)."""
    return f"px-{company}-{task_id}-{run_id}-"


def _run_of_container(name: str, company: str) -> str | None:
    """The run id in the name of one of the company's task containers (``container_prefix`` + try number), or None.
    Only run ids that start with their time, as the intake's do, so a container of a company whose name continues this
    one's (``acme`` and ``acme-x``) is never taken for one of this company's."""
    tasks = "|".join(map(re.escape, (*PIPELINE_TASKS, FINALIZE)))
    found = re.fullmatch(rf"px-{re.escape(company)}-(?:{tasks})-(\d{{8}}T\d{{6}}Z-[A-Za-z0-9_.-]+)-\d+", name)
    return found.group(1) if found else None


def stop_earlier_containers(context: MutableMapping[str, Any]) -> None:
    """``pre_execute`` of every task on the docker backend: ``check_run_id``, then clear what earlier tries and runs of
    the company left on this Docker daemon.

    - The containers of earlier tries of this task of this run are stopped and removed. When an Airflow worker dies
      (restart, deploy, kill), Docker keeps its container running, because the task's own clean-up (``on_kill``, the
      removal after ``execute``) never runs; the retry would then run a second copy of the task beside it, both writing
      the same files and run record. ``docker stop`` first, so the runner records the stop.
    - The exited containers of the company's earlier runs are removed. A try that Airflow stops from outside (the DAG
      run's ``dagrun_timeout``, the task marked failed or skipped) is killed 5 seconds after its SIGTERM, while
      ``on_kill``'s ``docker stop`` still gives the runner up to ``STOP_SECONDS`` to record the stop; the container
      then stays, exited, with what it staged, and no later try of that run comes to remove it. Running containers of
      other runs are left alone.

    Docker errors fail the try (and it is retried) rather than start a container beside one that may be running. A
    container left on another worker's Docker daemon is not seen here (deploy/README.md, section 14).
    """
    check_run_id(context)
    task, params = context["ti"].task, context.get("params") or {}
    company, run_id = params["company"], params["run_id"]
    prefix = container_prefix(company, task.task_id, run_id)
    cli = task.cli
    for container in cli.containers(all=True, filters={"name": f"px-{company}-"}):
        # the name filter matches any part of a name: keep only this company's own task containers
        names = [name.lstrip("/") for name in container.get("Names") or []]
        state = container.get("State") or container.get("Status") or "state unknown"
        if any(name.startswith(prefix) and name[len(prefix) :].isdigit() for name in names):
            log.warning(
                "stopping and removing container %s (%s) of an earlier try of this task: its worker stopped without "
                "stopping it",
                ", ".join(names),
                state,
            )
            cli.stop(container["Id"], timeout=STOP_SECONDS)
            cli.remove_container(container["Id"], force=True)
        elif state in ("exited", "dead") and any(
            _run_of_container(name, company) not in (None, run_id) for name in names
        ):
            log.warning(
                "removing container %s (%s) of an earlier run of %s: its try was stopped from outside (DAG run "
                "timeout, marked failed) before it could remove it",
                ", ".join(names),
                state,
                company,
            )
            cli.remove_container(container["Id"], force=True)


@functools.cache
def _docker_operator() -> type:
    """DockerOperator whose container is stopped with ``docker stop`` before it is removed, also when a try times out.

    The provider's ``auto_remove`` removes the container with SIGKILL as soon as ``execute`` is left, so on an
    ``execution_timeout`` the runner never got the SIGTERM on which it records the stop (and ``indirect`` uploads the
    filings it finished), and the provider's ``on_kill`` then failed on the removed container, which replaced the
    timeout as the task's error (provider 4.5.6). Created on first use: the ecs backend needs no docker provider.
    """
    from airflow.providers.docker.operators.docker import DockerOperator
    from docker.errors import APIError, NotFound

    class PxDockerOperator(DockerOperator):
        def execute(self, context: Any) -> Any:
            try:
                return super().execute(context)
            finally:
                self._end_container(remove=True)

        def on_kill(self) -> None:
            self._end_container(remove=False)

        def _end_container(self, remove: bool) -> None:
            if self.container is None:  # not created
                return
            try:
                self.cli.stop(self.container["Id"], timeout=STOP_SECONDS)  # no-op once it has exited
                if remove:
                    self.cli.remove_container(self.container["Id"], force=True)
            except NotFound:  # removed already (on_kill after a timeout)
                pass
            except APIError as exc:  # keep the task's own error; the next try removes what is left
                self.log.warning("could not stop or remove container %s: %s", self.container["Id"], exc)

    return PxDockerOperator


def _docker_task(task_id: str, env: dict[str, str], **kwargs: Any):
    return _docker_operator()(
        task_id=task_id,
        image=px_settings.get("px_image"),
        docker_url=px_settings.get("px_docker_url"),
        entrypoint=ENTRYPOINT,
        command=runner_args(task_id),
        environment=dict(env),
        container_name=container_prefix("{{ params.company }}", "{{ ti.task_id }}", "{{ params.run_id }}")
        + "{{ ti.try_number }}",
        auto_remove="never",  # PxDockerOperator removes it, after docker stop
        mount_tmp_dir=False,
        do_xcom_push=False,
        **kwargs,
    )


def _ecs_large_overrides() -> dict[str, Any]:
    overrides = px_settings.get_json_object("px_ecs_large_overrides")
    if "containerOverrides" in overrides:
        raise ValueError("Variable px_ecs_large_overrides: give task-level fields only (cpu, memory, ...)")
    for field in ("cpu", "memory"):  # RunTask takes them as strings ("8192"), JSON may give numbers
        if field in overrides:
            overrides[field] = str(overrides[field])
    return overrides


def _ecs_task(task_id: str, env: dict[str, str], ecs: Mapping[str, Any], **kwargs: Any):
    from airflow.providers.amazon.aws.operators.ecs import EcsRunTaskOperator

    container = ecs["container"]
    overrides: dict[str, Any] = {
        "containerOverrides": [
            {
                "name": container,
                "command": runner_args(task_id),
                "environment": [{"name": name, "value": value} for name, value in sorted(env.items())],
            }
        ]
    }
    if task_id == LARGE_TASK:
        overrides.update(ecs["large_overrides"])
    if task_id in TASK_ROLE:
        overrides["taskRoleArn"] = ecs[TASK_ROLE[task_id]]
    vpc = {
        "subnets": list(ecs["subnets"]),
        "securityGroups": list(ecs["security_groups"]),
        "assignPublicIp": "DISABLED",
    }
    log_group = ecs["log_group"]
    return EcsRunTaskOperator(
        task_id=task_id,
        cluster=ecs["cluster"],
        task_definition=ecs["task_definition"],
        launch_type="FARGATE",
        platform_version="LATEST",
        overrides=overrides,
        network_configuration={"awsvpcConfiguration": vpc},
        aws_conn_id=px_settings.AWS_CONN_ID,
        region_name=ecs["region"],
        container_name=container,
        awslogs_group=log_group,
        awslogs_region=ecs["region"] if log_group else None,
        awslogs_stream_prefix=f"ecs/{container}" if log_group else None,
        reattach=True,
        # never deferred, whatever [operators] default_deferrable says: Airflow 3.2 applies execution_timeout (the
        # px_task_timeout_hours stop) only while execute() runs on a worker, and a deferred try that dagrun_timeout
        # skips gets no on_kill, so its ECS task would keep running
        deferrable=False,
        **kwargs,
    )


def _ecs_settings() -> dict[str, Any]:
    ecs = {
        "cluster": px_settings.get("px_ecs_cluster"),
        "task_definition": px_settings.get("px_ecs_task_definition"),
        "subnets": px_settings.get_list("px_ecs_subnets"),
        # required: without one, RunTask puts the tasks in the VPC's default security group
        "security_groups": px_settings.get_list("px_ecs_security_groups"),
        # required: the task definition's own role reads no secret (deploy/iam/README.md)
        "task_role_sec": px_settings.get("px_ecs_task_role_sec"),
        "task_role_llm": px_settings.get("px_ecs_task_role_llm"),
        "container": px_settings.get("px_ecs_container"),
        "region": px_settings.get("px_region"),
        "large_overrides": _ecs_large_overrides(),
    }
    log_group = px_settings.get("px_ecs_log_group")
    ecs["log_group"] = None if log_group.lower() in ("none", "-", "off") else log_group
    required = ("cluster", "task_definition", "subnets", "security_groups", "task_role_sec", "task_role_llm")
    missing = [name for name in required if not ecs[name]]
    if missing:
        names = ", ".join(f"px_ecs_{name}" for name in missing)
        raise ValueError(f"px_task_backend is ecs but these Variables are not set: {names}")
    for name in ("task_role_sec", "task_role_llm"):
        if not _ROLE_ARN_RE.fullmatch(ecs[name]):
            raise ValueError(f"Variable px_ecs_{name} must be a role ARN (arn:aws:iam::<account>:role/<name>), "
                             f"got {ecs[name]!r}")
    return ecs


class MisconfiguredTask(BaseOperator):
    """Stands in for every task while a Variable is invalid: fails at once, without retries, with the reason.

    The DAG still imports, so the failure reaches the task's e-mail callback; an import error would reach no one.
    ``finalize`` raises ``RunFailed``: the failed first task has already sent the e-mail.
    """

    ui_color = "#f4c7c3"

    def __init__(self, *, problem: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.problem = problem

    def execute(self, context: Any) -> None:
        message = f"{DAG_ID} is misconfigured: {self.problem}"
        if self.task_id == FINALIZE:
            px_settings.raise_run_failed(context, message)
        raise AirflowFailException(message)


def _limits(read=px_settings.get) -> dict[str, Any]:
    """Task timeout, DAG run timeout and evidence slots (docker) from the Variables, or from the defaults."""
    def hours(key: str) -> timedelta:
        text = read(key)
        try:
            value = float(text)
        except ValueError:
            raise ValueError(f"Variable {key} must be a number of hours, got {text!r}") from None
        if not 0 < value < 24 * 365:
            raise ValueError(f"Variable {key} must be more than 0 hours and less than a year, got {text!r}")
        return timedelta(hours=value)

    slots = read("px_large_slots")
    if not slots.isdigit() or int(slots) < 1:
        raise ValueError(f"Variable px_large_slots must be a whole number of at least 1, got {slots!r}")
    return {"task": hours("px_task_timeout_hours"), "run": hours("px_run_timeout_hours"), "large_slots": int(slots)}


def _load_settings() -> tuple[str, dict[str, str], dict[str, Any] | None, dict[str, Any], str | None]:
    """(backend, task environment, ECS settings, limits, problem); problem is set when a Variable is invalid."""
    try:
        backend = px_settings.task_backend()
        env = px_settings.task_environment()
        ecs = _ecs_settings() if backend == "ecs" else None
        limits = _limits()
    except ValueError as exc:
        log.error("%s: %s. Every task fails until this is fixed.", DAG_ID, exc)
        return "", {}, None, _limits(px_settings.DEFAULTS.__getitem__), str(exc)
    return backend, env, ecs, limits, None


# Read when the file is parsed (px_settings explains when Variables are read).
BACKEND, _ENV, _ECS, LIMITS, SETTINGS_PROBLEM = _load_settings()
_SEC_POOL = px_settings.get("px_sec_pool")
_LARGE_QUEUE = px_settings.get("px_large_queue")
_QUEUE = px_settings.get("px_queue")


def make_task(task_id: str, **kwargs: Any):
    """One pipeline task on the configured backend."""
    kwargs.update(execution_timeout=LIMITS["task"], pre_execute=check_run_id)
    if task_id == SEC_TASK:
        kwargs["pool"] = _SEC_POOL
    if SETTINGS_PROBLEM:
        return MisconfiguredTask(task_id=task_id, problem=SETTINGS_PROBLEM, **kwargs)
    if BACKEND == "docker":
        kwargs["pre_execute"] = stop_earlier_containers
        if task_id == LARGE_TASK:
            # a queue only says where a task runs: the slots limit how many scans share the large machine(s)
            kwargs.update(queue=_LARGE_QUEUE, max_active_tis_per_dag=LIMITS["large_slots"])
        elif _QUEUE:
            # the px workers, whose role holds the task policy (unset: Airflow's default queue)
            kwargs["queue"] = _QUEUE
        return _docker_task(task_id, _ENV, **kwargs)
    return _ecs_task(task_id, _ENV, _ECS, **kwargs)


@dag(
    dag_id=DAG_ID,
    schedule=None,
    start_date=datetime(2026, 11, 1, tzinfo=timezone.utc),
    catchup=False,
    max_active_runs=4,
    dagrun_timeout=LIMITS["run"],
    on_failure_callback=px_settings.alert_on_run_timeout,
    params={
        "company": Param(
            type="string",
            pattern=px_intake.COMPANY_RE.pattern,
            description="Company key: the folder name in input/<company>/company.json",
        ),
        "run_id": Param(
            type="string",
            pattern=px_intake.RUN_ID_RE.pattern,
            description="Run id, <UTC time YYYYMMDDTHHMMSSZ>-<first 8 characters of the input ETag>",
        ),
    },
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
        "on_failure_callback": px_settings.alert_on_failure,
    },
    tags=["px", "company-evidence"],
    doc_md=__doc__,
)
def company_evidence():
    steps = [make_task(task_id) for task_id in PIPELINE_TASKS]
    # retried like the others: RunFailed (the run's verdict) is an AirflowFailException, which is never retried, so
    # a retry happens only when finalize itself did not work (a Docker or ECS error, S3, the task states unreadable)
    final = make_task(FINALIZE, trigger_rule=TriggerRule.ALL_DONE, post_execute=fail_if_run_failed)
    chain(*steps, final)


company_evidence()
