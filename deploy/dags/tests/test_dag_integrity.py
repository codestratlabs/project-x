"""DAG integrity: both DAG files import on Airflow 3 with the docker and amazon providers, and have the shape
deploy/INTERFACES.md fixes (task ids, dependencies, pools, trigger rules, the queue of ``evidence``, retries, the two
backends). Skipped as a whole when Airflow 3 or a provider is not installed."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import timedelta
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip(
    "airflow.sdk",
    reason="Airflow 3 is not installed (apache-airflow[docker,amazon]==3.2.2 with its constraints file)",
)
pytest.importorskip("airflow.providers.docker", reason="apache-airflow-providers-docker is not installed")
pytest.importorskip("airflow.providers.amazon", reason="apache-airflow-providers-amazon is not installed")

import jinja2  # noqa: E402
import px_intake  # noqa: E402
import px_settings  # noqa: E402
from airflow.models import DagBag  # noqa: E402
from airflow.sdk.exceptions import AirflowFailException  # noqa: E402

DAGS_DIR = Path(__file__).resolve().parents[1]
PIPELINE = ["aliases", "download", "finder", "records", "evidence", "indirect", "publish"]
ALL_TASKS = [*PIPELINE, "finalize"]
RUN_ID = "20261117T101500Z-1a2b3c4d"
ROLE_SEC = "arn:aws:iam::123456789012:role/px-task-sec"
ROLE_LLM = "arn:aws:iam::123456789012:role/px-task-llm"
ECS_VARS = {
    "px_task_backend": "ecs",
    "px_ecs_cluster": "px-cluster",
    "px_ecs_task_definition": "px-pipeline:3",
    "px_ecs_subnets": '["subnet-aaa", "subnet-bbb"]',
    "px_ecs_security_groups": "sg-111, sg-222",
    "px_ecs_task_role_sec": ROLE_SEC,
    "px_ecs_task_role_llm": ROLE_LLM,
}
# the task role of each task on ecs: the role of its secret, or none (the task definition's role, no secret)
ECS_ROLE = {"download": ROLE_SEC, "finder": ROLE_LLM, "indirect": ROLE_LLM}
CONTRACT_ENV = {
    "PX_BUCKET": "company-reports-data",
    "AWS_REGION": "us-east-2",
    "AWS_DEFAULT_REGION": "us-east-2",
    "PX_SECRET_SEC": "px/sec-user-agent",
    "PX_SECRET_LLM": "px/openrouter-api-key",
}


def load_bag(monkeypatch, **variables) -> DagBag:
    for name, value in variables.items():
        monkeypatch.setenv(f"AIRFLOW_VAR_{name.upper()}", value)
    return DagBag(dag_folder=str(DAGS_DIR), include_examples=False, safe_mode=False)


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, DAGS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def docker_bag(clean_px_variables) -> DagBag:
    bag = load_bag(clean_px_variables)
    assert bag.import_errors == {}
    return bag


@pytest.fixture
def ecs_bag(clean_px_variables) -> DagBag:
    bag = load_bag(clean_px_variables, **ECS_VARS)
    assert bag.import_errors == {}
    return bag


def dag_callbacks(dag) -> list:
    callbacks = dag.on_failure_callback
    return list(callbacks) if isinstance(callbacks, (list, tuple)) else [callbacks]


def render(dag, template, **context):
    return dag.get_template_env().from_string(template).render(**context)


# ---- both DAGs import ----------------------------------------------------------------------------------------------


def test_both_dags_import_without_errors(docker_bag):
    assert sorted(docker_bag.dag_ids) == ["company_evidence", "company_intake"]
    parsed = sorted(Path(path).name for path in docker_bag.file_last_changed)
    assert parsed == ["company_evidence.py", "company_intake.py"]  # .airflowignore keeps helpers and tests out


def test_serialized_dags_keep_pools_queue_and_trigger_rules(docker_bag):
    from airflow.serialization.serialized_objects import DagSerialization

    serialized = json.loads(json.dumps(DagSerialization.to_dict(docker_bag.dags["company_evidence"])))
    evidence = DagSerialization.from_dict(serialized)
    tasks = {t.task_id: t for t in evidence.tasks}
    assert sorted(tasks) == sorted(ALL_TASKS)
    assert tasks["download"].pool == "sec_api"
    assert tasks["evidence"].queue == "evidence_large"
    assert str(tasks["finalize"].trigger_rule) in ("all_done", "TriggerRule.ALL_DONE")
    assert tasks["finalize"].retries == 2 and tasks["publish"].retries == 2
    assert tasks["evidence"].max_active_tis_per_dag == 1 and tasks["download"].max_active_tis_per_dag is None
    assert {t.execution_timeout for t in tasks.values()} == {timedelta(hours=12)}
    assert evidence.dagrun_timeout == timedelta(hours=72)
    intake = DagSerialization.from_dict(DagSerialization.to_dict(docker_bag.dags["company_intake"]))
    assert sorted(t.task_id for t in intake.tasks) == ["plan_runs", "start_run.queue_run", "start_run.trigger"]
    assert intake.timetable.summary == "*/15 * * * *" and intake.max_active_runs == 1
    assert evidence.timetable.summary == "None" and evidence.max_active_runs == 4


# ---- company_evidence ----------------------------------------------------------------------------------------------


def test_evidence_dag_settings(docker_bag):
    dag = docker_bag.dags["company_evidence"]
    assert dag.schedule is None and type(dag.timetable).__name__ == "NullTimetable"
    assert dag.max_active_runs == 4
    assert dag.catchup is False
    params = {name: dag.params.get_param(name) for name in ("company", "run_id")}
    for name, param in params.items():
        assert param.schema["type"] == "string", name
        assert not param.has_value, f"{name} must come from dag_run.conf"
    assert params["company"].schema["pattern"] == px_intake.COMPANY_RE.pattern
    assert params["run_id"].schema["pattern"] == px_intake.RUN_ID_RE.pattern
    assert dag.dagrun_timeout == timedelta(hours=72)
    assert dag_callbacks(dag) == [px_settings.alert_on_run_timeout]  # a timed-out run has no failed task to e-mail


def test_evidence_tasks_and_dependencies(docker_bag):
    dag = docker_bag.dags["company_evidence"]
    assert sorted(dag.task_ids) == sorted(ALL_TASKS)
    for before, after in pairwise(ALL_TASKS):
        assert dag.get_task(after).upstream_task_ids == {before}, after
    assert dag.get_task("aliases").upstream_task_ids == set()
    assert dag.get_task("finalize").downstream_task_ids == set()
    assert [t.task_id for t in dag.leaves] == ["finalize"]


def test_evidence_trigger_rules_retries_and_callbacks(docker_bag):
    dag = docker_bag.dags["company_evidence"]
    for task_id in PIPELINE:
        task = dag.get_task(task_id)
        assert task.trigger_rule == "all_success", task_id
        assert task.retries == 2 and task.retry_delay == timedelta(minutes=5), task_id
        assert px_settings.alert_on_failure in task.on_failure_callback, task_id
    finalize = dag.get_task("finalize")
    assert finalize.trigger_rule == "all_done"
    # retried when it did not work itself; the run's verdict (RunFailed) is an AirflowFailException, never retried
    assert finalize.retries == 2 and finalize.retry_delay == timedelta(minutes=5)
    assert issubclass(px_settings.RunFailed, AirflowFailException)
    assert px_settings.alert_on_failure in finalize.on_failure_callback
    assert finalize._post_execute_hook.__name__ == "fail_if_run_failed"
    for task_id in ALL_TASKS:
        task = dag.get_task(task_id)
        assert task.execution_timeout == timedelta(hours=12), task_id  # a hung container is stopped, then retried
        # check_run_id first, then the containers an earlier try left behind (docker backend)
        assert task._pre_execute_hook.__name__ == "stop_earlier_containers", task_id


def test_evidence_pool_and_queue_on_docker(docker_bag):
    dag = docker_bag.dags["company_evidence"]
    for task_id in ALL_TASKS:
        task = dag.get_task(task_id)
        assert task.pool == ("sec_api" if task_id == "download" else "default_pool"), task_id
        assert task.queue == ("evidence_large" if task_id == "evidence" else "default"), task_id
        # the queue only routes; one evidence scan at a time on the large worker
        assert task.max_active_tis_per_dag == (1 if task_id == "evidence" else None), task_id


def test_docker_tasks_run_the_runner(docker_bag):
    from airflow.providers.docker.operators.docker import DockerOperator

    dag = docker_bag.dags["company_evidence"]
    for task_id in ALL_TASKS:
        task = dag.get_task(task_id)
        assert isinstance(task, DockerOperator), task_id
        assert task.image == "px-pipeline:latest"
        assert task.docker_url == "unix://var/run/docker.sock"
        assert task.entrypoint == ["python", "-m", "px_runner"]
        assert task.command == [task_id, "--company", "{{ params.company }}", "--run-id", "{{ params.run_id }}"]
        assert task.environment == CONTRACT_ENV
        # the task removes its container itself, after docker stop (test_docker_task_stops_its_container_...)
        assert type(task).__name__ == "PxDockerOperator" and task.auto_remove == "never"
        assert task.mount_tmp_dir is False


def test_docker_templates_render(docker_bag):
    dag = docker_bag.dags["company_evidence"]
    task = dag.get_task("evidence")
    context = {
        "params": {"company": "lungpacer", "run_id": RUN_ID},
        "ti": SimpleNamespace(task_id="evidence", try_number=2),
    }
    rendered = [render(dag, part, **context) for part in task.command]
    assert task.entrypoint + rendered == [
        "python", "-m", "px_runner", "evidence", "--company", "lungpacer", "--run-id", RUN_ID,
    ]
    assert render(dag, task.container_name, **context) == f"px-lungpacer-evidence-{RUN_ID}-2"


def test_variables_set_image_pool_queue_and_environment(clean_px_variables):
    bag = load_bag(
        clean_px_variables,
        px_image="123456789012.dkr.ecr.us-east-2.amazonaws.com/px-pipeline:2026-11-17",
        px_bucket="other-bucket",
        px_region="us-west-2",
        px_sec_pool="sec_shared",
        px_large_queue="big_worker",
        px_queue="px",
        px_docker_url="tcp://docker:2375",
        px_task_env='{"PX_WORKERS": 8, "PX_BUCKET": "ignored"}',
        px_task_timeout_hours="2.5",
        px_run_timeout_hours="30",
        px_large_slots="2",
    )
    assert bag.import_errors == {}
    dag = bag.dags["company_evidence"]
    assert dag.get_task("download").pool == "sec_shared"
    assert dag.get_task("evidence").queue == "big_worker" and dag.get_task("evidence").max_active_tis_per_dag == 2
    # every other task, finalize too, runs on the px workers (the ones whose role holds the task policy)
    assert {t.task_id for t in dag.tasks if t.queue == "px"} == set(ALL_TASKS) - {"evidence"}
    assert dag.get_task("aliases").execution_timeout == timedelta(hours=2.5)
    assert dag.dagrun_timeout == timedelta(hours=30)
    task = dag.get_task("records")
    assert task.image.endswith("/px-pipeline:2026-11-17")
    assert task.docker_url == "tcp://docker:2375"
    assert task.environment["PX_BUCKET"] == "other-bucket"  # the contract's settings win over px_task_env
    assert task.environment["AWS_REGION"] == "us-west-2" and task.environment["PX_WORKERS"] == "8"


@pytest.mark.parametrize(
    ("variables", "message"),
    [
        ({"px_task_backend": "fargate"}, "px_task_backend"),
        (
            {"px_task_backend": "ecs"},
            "px_ecs_cluster, px_ecs_task_definition, px_ecs_subnets, px_ecs_security_groups, px_ecs_task_role_sec, "
            "px_ecs_task_role_llm",
        ),
        # without a security group RunTask would use the VPC's default group
        ({**ECS_VARS, "px_ecs_security_groups": ""}, "not set: px_ecs_security_groups$"),
        ({**ECS_VARS, "px_ecs_task_role_llm": " "}, "not set: px_ecs_task_role_llm$"),
        ({**ECS_VARS, "px_ecs_task_role_sec": "px-task-sec"}, "px_ecs_task_role_sec must be a role ARN"),
        ({**ECS_VARS, "px_ecs_large_overrides": "{cpu: 1}"}, "px_ecs_large_overrides"),
        ({"px_task_env": '{"OPENROUTER_API_KEY": "x"}'}, "Secrets Manager"),
        ({"px_task_env": "[1, 2]"}, "JSON object"),
        ({"px_task_timeout_hours": "0"}, "px_task_timeout_hours"),
        ({"px_run_timeout_hours": "three days"}, "px_run_timeout_hours"),
        ({"px_large_slots": "1.5"}, "px_large_slots"),
    ],
)
def test_bad_settings_fail_every_task_with_the_reason(clean_px_variables, variables, message):
    """The file still imports (an import error would e-mail no one); each task fails at once with the reason."""
    from airflow.sdk.exceptions import AirflowFailException

    bag = load_bag(clean_px_variables, **variables)
    assert bag.import_errors == {}
    dag = bag.dags["company_evidence"]
    assert sorted(dag.task_ids) == sorted(ALL_TASKS)
    for before, after in pairwise(ALL_TASKS):
        assert dag.get_task(after).upstream_task_ids == {before}
    assert dag.get_task("download").pool == "sec_api" and dag.get_task("finalize").trigger_rule == "all_done"
    assert dag.dagrun_timeout == timedelta(hours=72)  # the defaults stand in
    for task_id in ALL_TASKS:
        task = dag.get_task(task_id)
        assert type(task).__name__ == "MisconfiguredTask", task_id
        assert task.execution_timeout == timedelta(hours=12), task_id
        with pytest.raises(AirflowFailException, match=message) as raised:
            task.execute({})
        assert isinstance(raised.value, px_settings.RunFailed) == (task_id == "finalize")


def test_ecs_tasks(ecs_bag):
    from airflow.providers.amazon.aws.operators.ecs import EcsRunTaskOperator

    dag = ecs_bag.dags["company_evidence"]
    assert sorted(dag.task_ids) == sorted(ALL_TASKS)
    for before, after in pairwise(ALL_TASKS):
        assert dag.get_task(after).upstream_task_ids == {before}
    for task_id in ALL_TASKS:
        task = dag.get_task(task_id)
        assert isinstance(task, EcsRunTaskOperator), task_id
        assert task.cluster == "px-cluster" and task.task_definition == "px-pipeline:3"
        assert task.launch_type == "FARGATE" and task.reattach is True
        # never deferred, also where [operators] default_deferrable is on: execution_timeout and on_kill need the
        # try's execute() on a worker (Airflow 3.2 does not apply execution_timeout to a deferred try)
        assert task.deferrable is False
        assert task.region_name == "us-east-2"
        assert task.network_configuration == {
            "awsvpcConfiguration": {
                "subnets": ["subnet-aaa", "subnet-bbb"],
                "securityGroups": ["sg-111", "sg-222"],
                "assignPublicIp": "DISABLED",
            }
        }
        (container,) = task.overrides["containerOverrides"]
        assert container["name"] == "px" and task.container_name == "px"
        assert container["command"] == [task_id, "--company", "{{ params.company }}", "--run-id", "{{ params.run_id }}"]
        assert {e["name"]: e["value"] for e in container["environment"]} == CONTRACT_ENV
        assert task.awslogs_group == "/ecs/px-pipeline" and task.awslogs_stream_prefix == "ecs/px"
        assert task.pool == ("sec_api" if task_id == "download" else "default_pool")
        assert task.queue == "default"  # the larger size comes from the task override, not a worker queue
        assert task.max_active_tis_per_dag is None  # each evidence task gets its own Fargate task
        assert task.execution_timeout == timedelta(hours=12)  # on timeout, on_kill stops the ECS task
        assert task._pre_execute_hook.__name__ == "check_run_id"  # reattach=True finds an earlier try's ECS task
        large = {"cpu": "8192", "memory": "61440", "ephemeralStorage": {"sizeInGiB": 200}}
        extra = {k: v for k, v in task.overrides.items() if k != "containerOverrides"}
        role = {"taskRoleArn": ECS_ROLE[task_id]} if task_id in ECS_ROLE else {}
        assert extra == (large if task_id == "evidence" else {}) | role, task_id
    assert dag.get_task("finalize").trigger_rule == "all_done" and dag.get_task("finalize").retries == 2
    assert dag.get_task("download").retries == 2


def test_ecs_task_roles_follow_the_secrets_the_runner_reads(evidence_module, monkeypatch):
    """A task reads the secret of the runner's TASK_SECRET (deploy/runner) and runs as the role that may read only that
    secret; a task that reads none keeps the task definition's role. A difference would fail the task (AccessDenied)."""
    monkeypatch.syspath_prepend(str(DAGS_DIR.parent / "runner"))
    from px_runner import secrets, tasks

    role_of_secret = {secrets.SEC_USER_AGENT: "task_role_sec", secrets.OPENROUTER_API_KEY: "task_role_llm"}
    assert evidence_module.TASK_ROLE == {task: role_of_secret[name] for task, name in tasks.TASK_SECRET.items()}
    assert set(evidence_module.TASK_ROLE) < set(ALL_TASKS)


def test_ecs_large_overrides_and_logs_from_variables(clean_px_variables):
    bag = load_bag(
        clean_px_variables,
        **ECS_VARS,
        px_ecs_large_overrides='{"cpu": 16384, "memory": "122880"}',
        px_ecs_log_group="none",
        px_ecs_container="pipeline",
    )
    assert bag.import_errors == {}
    dag = bag.dags["company_evidence"]
    evidence = dag.get_task("evidence")
    assert evidence.overrides["cpu"] == "16384" and evidence.overrides["memory"] == "122880"
    assert evidence.overrides["containerOverrides"][0]["name"] == "pipeline"
    assert evidence.awslogs_group is None and evidence.awslogs_stream_prefix is None


def test_ecs_run_task_request(ecs_bag, fake_aws):
    """The exact RunTask request, checked by botocore against the ECS API model (Stubber: no AWS call)."""
    import boto3
    from botocore.stub import Stubber

    dag = ecs_bag.dags["company_evidence"]
    for task_id in ("download", "evidence", "finder"):
        task = dag.get_task(task_id)
        task.render_template_fields({"params": {"company": "lungpacer", "run_id": RUN_ID}})
        client = boto3.client("ecs", region_name="us-east-2")
        container = {
            "name": "px",
            "command": [task_id, "--company", "lungpacer", "--run-id", RUN_ID],
            "environment": [{"name": k, "value": v} for k, v in sorted(CONTRACT_ENV.items())],
        }
        overrides = {"containerOverrides": [container]}
        if task_id == "evidence":
            overrides |= {"cpu": "8192", "memory": "61440", "ephemeralStorage": {"sizeInGiB": 200}}
        if task_id in ECS_ROLE:  # the role that may read this task's secret, and no other
            overrides["taskRoleArn"] = ECS_ROLE[task_id]
        expected = {
            "cluster": "px-cluster",
            "taskDefinition": "px-pipeline:3",
            "overrides": overrides,
            "startedBy": "airflow",
            "launchType": "FARGATE",
            "platformVersion": "LATEST",
            "networkConfiguration": {
                "awsvpcConfiguration": {
                    "subnets": ["subnet-aaa", "subnet-bbb"],
                    "assignPublicIp": "DISABLED",
                    "securityGroups": ["sg-111", "sg-222"],
                }
            },
        }
        arn = f"arn:aws:ecs:us-east-2:123456789012:task/px-cluster/{task_id}"
        with Stubber(client) as stub:
            stub.add_response("run_task", {"tasks": [{"taskArn": arn}], "failures": []}, expected)
            task.__dict__["client"] = client  # the operator's cached boto3 client
            task._start_task()
            stub.assert_no_pending_responses()
        assert task.arn == arn


def test_ecs_command_renders(ecs_bag):
    dag = ecs_bag.dags["company_evidence"]
    command = dag.get_task("publish").overrides["containerOverrides"][0]["command"]
    context = {"params": {"company": "horizon3_ai", "run_id": RUN_ID}}
    assert [render(dag, part, **context) for part in command] == [
        "publish", "--company", "horizon3_ai", "--run-id", RUN_ID,
    ]


# ---- finalize marks the run failed when a pipeline task did not succeed ---------------------------------------------


class FakeTi:
    dag_id, run_id = "company_evidence", RUN_ID

    def __init__(self, states=None, error=None):
        self.states, self.error = states, error

    def get_task_states(self, dag_id, task_ids, run_ids):
        if self.error:
            raise self.error
        assert dag_id == "company_evidence" and run_ids == [RUN_ID] and task_ids == PIPELINE
        return {RUN_ID: self.states}


@pytest.fixture
def evidence_module(clean_px_variables):
    return load_module("company_evidence_under_test", "company_evidence.py")


def test_finalize_passes_when_every_task_succeeded_or_skipped(evidence_module):
    states = {task: "success" for task in PIPELINE} | {"indirect": "skipped"}
    evidence_module.fail_if_run_failed({"ti": FakeTi(states)})


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        (
            {"aliases": "success", "download": "failed"} | {t: "upstream_failed" for t in PIPELINE[2:]},
            "download failed",
        ),
        ({task: "success" for task in PIPELINE if task != "publish"}, "publish not run"),
    ],
)
def test_finalize_fails_the_run_when_a_task_did_not_succeed(evidence_module, states, expected):
    context = {"ti": FakeTi(states)}
    with pytest.raises(px_settings.RunFailed, match=expected):
        evidence_module.fail_if_run_failed(context)
    assert context[px_settings.RUN_FAILED_KEY] is True  # the failure callback sees the verdict without "exception"


def test_finalize_check_fails_when_it_cannot_read_the_states(evidence_module):
    """The runner's finalize exits 0 for a failed run too, so the check must not let an API error pass the run."""
    context = {"ti": FakeTi(error=RuntimeError("API down"))}
    with pytest.raises(RuntimeError, match="cannot read its task states") as raised:
        evidence_module.fail_if_run_failed(context)
    assert not isinstance(raised.value, px_settings.RunFailed) and px_settings.RUN_FAILED_KEY not in context


@pytest.mark.parametrize("task_id", ["aliases", "finalize"])
def test_every_task_checks_that_the_conf_run_id_is_the_dag_run_id(evidence_module, task_id):
    from airflow.sdk.exceptions import AirflowFailException

    ti = SimpleNamespace(task_id=task_id)
    same = {"ti": ti, "run_id": RUN_ID, "params": {"company": "lungpacer", "run_id": RUN_ID}}
    evidence_module.check_run_id(same)
    other = {**same, "run_id": "manual__2026-11-17T16:26:21+00:00"}  # the UI's default run id
    with pytest.raises(AirflowFailException, match="is not this DAG run's id") as raised:
        evidence_module.check_run_id(other)
    # finalize must not write the conf's record, and sends no second e-mail
    assert isinstance(raised.value, px_settings.RunFailed) == (task_id == "finalize")
    assert other.get(px_settings.RUN_FAILED_KEY, False) == (task_id == "finalize")


# ---- docker: containers that earlier tries left behind -------------------------------------------------------------


class FakeDockerClient:
    """The Docker API calls of stop_earlier_containers, on a fixed list of containers (a name, or (name, state))."""

    def __init__(self, names):
        listed = [(name, "running") if isinstance(name, str) else name for name in names]
        self.listed = [{"Id": f"id-{i}", "Names": [f"/{name}"], "State": state} for i, (name, state) in enumerate(listed)]
        self.calls = []

    def containers(self, all, filters):
        assert all is True  # stopped ones too: they still hold the name
        self.calls.append(("list", filters["name"]))
        return [c for c in self.listed if filters["name"] in c["Names"][0]]  # Docker matches any part of a name

    def stop(self, container, timeout):
        self.calls.append(("stop", container, timeout))

    def remove_container(self, container, force):
        self.calls.append(("remove", container, force))


def docker_context(cli, run_id=RUN_ID):
    task = SimpleNamespace(task_id="evidence", cli=cli)
    return {"ti": SimpleNamespace(task_id="evidence", task=task), "run_id": run_id,
            "params": {"company": "lungpacer", "run_id": RUN_ID}}


def test_docker_task_stops_and_removes_the_containers_of_earlier_tries(evidence_module):
    """A worker that died left its container running; the retry stops it (so its runner records the stop) and removes
    it before it starts its own, so two copies of the task never write the same files and record."""
    prefix = evidence_module.container_prefix("lungpacer", "evidence", RUN_ID)
    assert prefix + "2" == f"px-lungpacer-evidence-{RUN_ID}-2"  # the container_name of try 2 (test_docker_templates_render)
    cli = FakeDockerClient([
        prefix + "1",                                         # an earlier try
        prefix + "3",                                         # this try's own name, left by a crash in the same try
        f"px-lungpacer-evidence-{RUN_ID}-2-1",                # run id {RUN_ID}-2, another run
        f"px-lungpacer-records-{RUN_ID}-1",                   # another task
        "px-lungpacer-evidence-20261118T000000Z-1a2b3c4d-1",  # another run
        f"x{prefix}1",
    ])
    evidence_module.stop_earlier_containers(docker_context(cli))
    stop = evidence_module.STOP_SECONDS
    assert cli.calls == [
        ("list", "px-lungpacer-"),
        ("stop", "id-0", stop), ("remove", "id-0", True),
        ("stop", "id-1", stop), ("remove", "id-1", True),
    ]


def test_docker_task_removes_exited_containers_that_earlier_runs_left(evidence_module):
    """A try stopped from outside (dagrun_timeout, marked failed) is killed 5 s after its SIGTERM, before it removes its
    container, and no later try of that run comes: the company's next run removes the exited container, and leaves
    the running ones of other runs and every container of another company alone."""
    old = "20261110T080000Z-9f8e7d6c"
    cli = FakeDockerClient([
        (f"px-lungpacer-evidence-{old}-1", "exited"),       # stopped from outside: removed
        (f"px-lungpacer-indirect-{old}-3", "dead"),         # another task of that run: removed
        (f"px-lungpacer-finalize-{RUN_ID}-1", "exited"),    # this run's finalize: not this task's, not an earlier run
        (f"px-lungpacer-finder-{old}-2", "running"),        # running: left alone
        (f"px-lungpacer-x-evidence-{old}-1", "exited"),     # company lungpacer-x
        (f"px-lungpacer-evidence-manual__{old}-1", "exited"),  # not a container name of an intake run id
        (f"px-lungpacer-evidence-{RUN_ID}-1", "exited"),    # this task's earlier try: stopped, then removed
    ])
    evidence_module.stop_earlier_containers(docker_context(cli))
    stop = evidence_module.STOP_SECONDS
    assert cli.calls == [
        ("list", "px-lungpacer-"),
        ("remove", "id-0", True),
        ("remove", "id-1", True),
        ("stop", "id-6", stop), ("remove", "id-6", True),
    ]


def test_docker_task_stops_its_container_before_removing_it(docker_bag, monkeypatch):
    """On a timeout the provider's auto_remove would SIGKILL the container before on_kill, so the runner could not record
    the stop (nor indirect upload its finished filings), and on_kill would fail on the removed container. The task
    gives it docker stop first; on_kill afterwards finds it gone without an error of its own."""
    from airflow.providers.docker.operators.docker import DockerOperator
    from airflow.sdk.exceptions import AirflowTaskTimeout
    from docker.errors import NotFound

    calls, gone = [], set()

    class Cli:
        def stop(self, container, timeout):
            calls.append(("stop", container, timeout))
            if container in gone:
                raise NotFound("No such container")

        def remove_container(self, container, force):
            calls.append(("remove", container, force))
            gone.add(container)

    def timed_out(self, context):
        self.container = {"Id": "c1"}
        raise AirflowTaskTimeout("Timeout")

    monkeypatch.setattr(DockerOperator, "execute", timed_out)
    task = docker_bag.dags["company_evidence"].get_task("indirect")
    task.__dict__["hook"] = SimpleNamespace(api_client=Cli(), client_created=True)
    with pytest.raises(AirflowTaskTimeout):
        task.execute({})
    task.on_kill()  # what the task runner does next on a timeout
    stop = sys.modules[type(task).__module__].STOP_SECONDS  # the DAG file as DagBag loaded it
    assert calls == [("stop", "c1", stop), ("remove", "c1", True), ("stop", "c1", stop)]


def test_docker_task_checks_the_run_id_before_it_touches_containers(evidence_module):
    cli = FakeDockerClient([evidence_module.container_prefix("lungpacer", "evidence", RUN_ID) + "1"])
    with pytest.raises(AirflowFailException, match="is not this DAG run's id"):
        evidence_module.stop_earlier_containers(docker_context(cli, run_id="manual__2026-11-17T16:26:21+00:00"))
    assert cli.calls == []


# ---- Variables that cannot be read ---------------------------------------------------------------------------------


class FakeComms:
    """The task's channel to the API server: answers GetVariable from a dict, or fails like an unreachable server."""

    def __init__(self, values=None, broken=False):
        self.values, self.broken = values or {}, broken

    def send(self, msg):
        from airflow.sdk.exceptions import ErrorType
        from airflow.sdk.execution_time.comms import ErrorResponse, VariableResult

        if self.broken:
            raise ConnectionError("API server unreachable")
        if msg.key in self.values:
            return VariableResult(key=msg.key, value=self.values[msg.key])
        return ErrorResponse(error=ErrorType.VARIABLE_NOT_FOUND, detail={"key": msg.key})


def test_a_variable_that_cannot_be_read_is_an_error_not_its_default(clean_px_variables):
    """Airflow 3.2's API backend answers "not set" when the call fails; get() asks again before it uses a default."""
    from airflow.sdk.execution_time import task_runner

    clean_px_variables.setattr(task_runner, "SUPERVISOR_COMMS", FakeComms({"px_task_backend": "ecs"}), raising=False)
    assert px_settings.get("px_task_backend") == "ecs"
    assert px_settings.get("px_image") == "px-pipeline:latest"  # not set: its default
    clean_px_variables.setattr(task_runner, "SUPERVISOR_COMMS", FakeComms(broken=True), raising=False)
    with pytest.raises(px_settings.VariableUnreadable, match="px_task_backend"):
        px_settings.get("px_task_backend")
    with pytest.raises(px_settings.VariableUnreadable, match="px_task_env"):  # PX_NPORT_DB would be lost silently
        px_settings.task_environment()
    clean_px_variables.setenv("AIRFLOW_VAR_PX_TASK_BACKEND", "ecs")  # another backend has it: no second question
    assert px_settings.get("px_task_backend") == "ecs"


# ---- failure e-mail ------------------------------------------------------------------------------------------------


@pytest.fixture
def sent(monkeypatch):
    """Replace SmtpNotifier; render its templates like Airflow does (StrictUndefined) and keep the result."""
    messages = []

    class FakeNotifier:
        def __init__(self, *, to, subject, html_content):
            self.to, self.subject, self.html_content = to, subject, html_content

        def __call__(self, context):
            env = jinja2.Environment(undefined=jinja2.StrictUndefined)
            subject = env.from_string(self.subject).render(context)
            messages.append((self.to, subject, env.from_string(self.html_content).render(context)))

    monkeypatch.setattr("airflow.providers.smtp.notifications.smtp.SmtpNotifier", FakeNotifier)
    return messages


def failure_context(**extra):
    ti = SimpleNamespace(dag_id="company_evidence", task_id="download", try_number=3, log_url="http://airflow/log?x=1")
    context = {
        "ti": ti,
        "run_id": RUN_ID,
        "params": {"company": "lungpacer", "run_id": RUN_ID},
        "exception": RuntimeError("container exited with 1 <html>"),
    }
    return context | extra


def test_failure_email(clean_px_variables, sent):
    clean_px_variables.setenv("AIRFLOW_VAR_PX_ALERT_EMAILS", "ops@example.com; data@example.com")
    px_settings.alert_on_failure(failure_context())
    ((to, subject, body),) = sent
    assert to == ["ops@example.com", "data@example.com"]
    assert subject == f"[px] company_evidence: download failed for lungpacer ({RUN_ID})"
    assert f"s3://company-reports-data/runs/lungpacer/{RUN_ID}.json" in body
    assert "container exited with 1 &lt;html&gt;" in body and "try 3" in body


def test_failure_email_for_the_intake(clean_px_variables, sent):
    clean_px_variables.setenv("AIRFLOW_VAR_PX_ALERT_EMAILS", '["ops@example.com"]')
    ti = SimpleNamespace(dag_id="company_intake", task_id="plan_runs", try_number=2, log_url="http://airflow/log")
    px_settings.alert_on_failure(failure_context(ti=ti, params={}, run_id="scheduled__2026-11-17T10:15:00+00:00"))
    ((_, subject, body),) = sent
    assert subject == "[px] company_intake: plan_runs failed (scheduled__2026-11-17T10:15:00+00:00)"
    assert "Company: -" in body and "Run record: -" in body
    trigger = SimpleNamespace(
        dag_id="company_intake", task_id="start_run.trigger", try_number=3, log_url="x", rendered_map_index="lungpacer"
    )
    px_settings.alert_on_failure(failure_context(ti=trigger, params={}, run_id="scheduled__x"))
    assert sent[1][1] == "[px] company_intake: start_run.trigger failed for lungpacer (scheduled__x)"


def test_failure_email_without_exception_in_the_context(clean_px_variables, sent):
    """Airflow's in-process runner (airflow dags test) calls the callback without context["exception"]."""
    clean_px_variables.setenv("AIRFLOW_VAR_PX_ALERT_EMAILS", "ops@example.com")
    context = failure_context()
    del context["exception"]
    px_settings.alert_on_failure(context)
    ((_, subject, body),) = sent
    assert subject.startswith("[px] company_evidence: download failed") and "see the task log" in body


def test_no_email_without_recipients_or_for_the_run_verdict(clean_px_variables, sent):
    px_settings.alert_on_failure(failure_context())
    clean_px_variables.setenv("AIRFLOW_VAR_PX_ALERT_EMAILS", "ops@example.com")
    px_settings.alert_on_failure(failure_context(exception=px_settings.RunFailed("run failed")))
    verdict = failure_context(**{px_settings.RUN_FAILED_KEY: True})
    del verdict["exception"]
    px_settings.alert_on_failure(verdict)
    assert sent == []


def test_failure_email_errors_are_swallowed(clean_px_variables, monkeypatch):
    clean_px_variables.setenv("AIRFLOW_VAR_PX_ALERT_EMAILS", "ops@example.com")

    def broken(**kwargs):
        raise ConnectionError("no SMTP")

    monkeypatch.setattr("airflow.providers.smtp.notifications.smtp.SmtpNotifier", broken)
    px_settings.alert_on_failure(failure_context())


def test_finalize_container_failure_is_an_email_but_the_run_verdict_is_not(clean_px_variables, sent):
    """The runner's finalize exits 0 once it has recorded a failed run (RunFailed then fails the task, no e-mail), so a
    failed finalize container means finalize itself did not work: that one is e-mailed."""
    clean_px_variables.setenv("AIRFLOW_VAR_PX_ALERT_EMAILS", "ops@example.com")
    ti = SimpleNamespace(dag_id="company_evidence", task_id="finalize", try_number=1, log_url="x")
    px_settings.alert_on_failure(failure_context(ti=ti, exception=px_settings.RunFailed("run failed (download failed)")))
    assert sent == []
    px_settings.alert_on_failure(failure_context(ti=ti, exception=RuntimeError("Docker container failed: 137")))
    assert [m[1] for m in sent] == [f"[px] company_evidence: finalize failed for lungpacer ({RUN_ID})"]


def run_failure_context(**extra):
    """A DAG-level failure callback's context as the DAG processor builds it with the run's last task."""
    dag_run = SimpleNamespace(dag_id="company_evidence", run_id=RUN_ID, conf={"company": "lungpacer", "run_id": RUN_ID})
    context = {
        "dag": SimpleNamespace(dag_id="company_evidence"),
        "dag_run": dag_run,
        "run_id": RUN_ID,
        "ti": SimpleNamespace(task_id="evidence"),
        "reason": "timed_out",
    }
    return context | extra


def test_run_timeout_email_for_company_evidence(clean_px_variables, sent):
    clean_px_variables.setenv("AIRFLOW_VAR_PX_ALERT_EMAILS", "ops@example.com")
    px_settings.alert_on_run_timeout(run_failure_context(reason="task_failure"))  # the failed task has e-mailed
    assert sent == []
    px_settings.alert_on_run_timeout(run_failure_context())
    ((to, subject, body),) = sent
    assert to == ["ops@example.com"]
    assert subject == f"[px] company_evidence: run failed for lungpacer, timed_out ({RUN_ID})"
    assert f"s3://company-reports-data/runs/lungpacer/{RUN_ID}.json" in body and "Last task: evidence" in body
    assert "px_run_timeout_hours" in body


def test_run_failure_email_for_the_intake_with_the_smallest_context(clean_px_variables, sent):
    """Without a task instance the DAG processor passes only dag, run_id and reason."""
    clean_px_variables.setenv("AIRFLOW_VAR_PX_ALERT_EMAILS", "ops@example.com")
    context = {"dag": SimpleNamespace(dag_id="company_intake"), "run_id": "scheduled__x", "reason": "task_failure"}
    px_settings.alert_on_run_failure(context)
    ((_, subject, body),) = sent
    assert subject == "[px] company_intake: run failed, task_failure (scheduled__x)"
    assert "Company: -" in body and "Last task: -" in body
    # the task that crashed, as the DAG processor passes it (Airflow 3.2.2): its map index, but no rendered map index
    trigger = SimpleNamespace(task_id="start_run.trigger", map_index=2)
    px_settings.alert_on_run_failure({**context, "ti": trigger})
    assert sent[1][1] == "[px] company_intake: run failed, task_failure (scheduled__x)"
    assert "Last task: start_run.trigger, map index 2" in sent[1][2] and "Run record: -" in sent[1][2]
    planner = SimpleNamespace(task_id="plan_runs", map_index=-1)  # not mapped: no map index in the e-mail
    px_settings.alert_on_run_failure({**context, "ti": planner})
    assert "Last task: plan_runs</p>" in sent[2][2].replace("\n", "")


# ---- company_intake ------------------------------------------------------------------------------------------------


def test_intake_dag_settings(docker_bag):
    dag = docker_bag.dags["company_intake"]
    assert type(dag.timetable).__name__ == "CronTriggerTimetable"
    assert dag.timetable.expression == "*/15 * * * *"
    assert dag.catchup is False
    assert dag.max_active_runs == 1


def test_intake_tasks_and_dependencies(docker_bag):
    from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator

    dag = docker_bag.dags["company_intake"]
    assert sorted(dag.task_ids) == ["plan_runs", "start_run.queue_run", "start_run.trigger"]
    assert dag.get_task("start_run.queue_run").upstream_task_ids == {"plan_runs"}
    assert dag.get_task("start_run.trigger").upstream_task_ids == {"start_run.queue_run"}
    assert type(dag.task_group_dict["start_run"]).__name__ == "MappedTaskGroup"
    trigger = dag.get_task("start_run.trigger")
    assert isinstance(trigger, TriggerDagRunOperator)
    assert trigger.trigger_dag_id == "company_evidence"
    assert trigger.wait_for_completion is False and trigger.skip_when_already_exists is True
    assert trigger.logical_date is None and trigger.openlineage_inject_parent_info is False
    assert trigger.retries == 2 and dag.get_task("start_run.queue_run").retries == 2
    # one e-mail per failed intake run, from the DAG: a failed trigger call never reaches a task callback
    assert dag_callbacks(dag) == [px_settings.alert_on_run_failure]
    for task in dag.tasks:
        assert not task.on_failure_callback, task.task_id
        assert task.trigger_rule == "all_success" and task.pool == "default_pool"


class FakeIntakeTi:
    def __init__(self, paused=False, active_run_ids=(), broken=False):
        self.paused, self.active, self.broken = paused, set(active_run_ids), broken
        self.asked = []

    def get_dag(self, dag_id):
        if self.broken:
            raise RuntimeError("API down")
        return SimpleNamespace(dag_id=dag_id, is_paused=self.paused)

    def get_dr_count(self, dag_id, run_ids, states):
        if self.broken:
            raise RuntimeError("API down")
        assert dag_id == "company_evidence" and sorted(states) == ["queued", "running"]
        self.asked += run_ids
        return sum(1 for run_id in run_ids if run_id in self.active)


@pytest.fixture
def intake(clean_px_variables, fake_aws, monkeypatch):
    """company_intake's task callables on a moto bucket, with a fake task instance."""
    moto = pytest.importorskip("moto", reason="moto is not installed")
    import boto3

    clean_px_variables.setenv("AIRFLOW_VAR_PX_BUCKET", "intake-test")
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-2")
        s3.create_bucket(Bucket="intake-test", CreateBucketConfiguration={"LocationConstraint": "us-east-2"})
        module = load_module("company_intake_under_test", "company_intake.py")
        dag = module.company_intake()
        state = SimpleNamespace(module=module, s3=s3, ti=FakeIntakeTi(), context={})

        def context():
            state.context["ti"] = state.ti
            return state.context

        monkeypatch.setattr(module, "get_current_context", context)
        state.plan_runs = dag.get_task("plan_runs").python_callable
        state.queue_run = dag.get_task("start_run.queue_run").python_callable
        yield state


def test_intake_plans_and_queues_on_s3(intake):
    intake.s3.put_object(Bucket="intake-test", Key="input/lungpacer/company.json", Body=b'{"name": "Lungpacer"}')
    intake.s3.put_object(Bucket="intake-test", Key="input/horizon3_ai/company.json", Body=b'{"name": "Horizon3"}')
    plans = intake.plan_runs()
    assert [(p["company"], p["reason"]) for p in plans] == [("horizon3_ai", "new"), ("lungpacer", "new")]
    out = intake.queue_run(plans[1])
    assert out == {"run_id": plans[1]["run_id"], "conf": {"company": "lungpacer", "run_id": plans[1]["run_id"]}}
    assert intake.context["px_company"] == "lungpacer"
    key = f"runs/lungpacer/{plans[1]['run_id']}.json"
    assert json.loads(intake.s3.get_object(Bucket="intake-test", Key=key)["Body"].read())["status"] == "queued"
    assert [p["company"] for p in intake.plan_runs()] == ["horizon3_ai"]  # lungpacer is queued now


def test_a_cleared_queue_run_leaves_the_record_of_a_started_run(intake):
    """Clearing start_run.queue_run after its run started must not reset the record to queued: the entries of the
    tasks that finished would be lost, and finalize would fail the run."""
    intake.s3.put_object(Bucket="intake-test", Key="input/lungpacer/company.json", Body=b"{}")
    (plan,) = intake.plan_runs()
    first = intake.queue_run(plan)
    key = f"runs/lungpacer/{plan['run_id']}.json"
    started = {**json.loads(intake.s3.get_object(Bucket="intake-test", Key=key)["Body"].read()), "status": "running",
               "tasks": {"aliases": {"status": "succeeded"}}}
    intake.s3.put_object(Bucket="intake-test", Key=key, Body=json.dumps(started).encode())
    assert intake.queue_run(plan) == first  # the trigger then skips the run that exists
    assert json.loads(intake.s3.get_object(Bucket="intake-test", Key=key)["Body"].read()) == started


def test_intake_plans_nothing_while_company_evidence_is_paused(intake):
    intake.s3.put_object(Bucket="intake-test", Key="input/lungpacer/company.json", Body=b"{}")
    intake.ti = FakeIntakeTi(paused=True)
    assert intake.plan_runs() == []
    intake.ti = FakeIntakeTi(broken=True)  # cannot tell: fails (and is retried), plans nothing
    with pytest.raises(RuntimeError, match="API down"):
        intake.plan_runs()


def test_intake_asks_airflow_about_stale_looking_runs(intake):
    intake.s3.put_object(Bucket="intake-test", Key="input/lungpacer/company.json", Body=b"{}")
    old = "20261101T000000Z-1a2b3c4d"
    record = {
        "company": "lungpacer",
        "run_id": old,
        "status": "queued",
        "input_etag": "x",
        "created_at": "2026-11-01T00:00:00Z",
    }
    intake.s3.put_object(Bucket="intake-test", Key=f"runs/lungpacer/{old}.json", Body=json.dumps(record).encode())
    intake.ti = FakeIntakeTi(active_run_ids=[old])
    assert intake.plan_runs() == []  # still queued in Airflow: not doubled
    assert intake.ti.asked == [old]
    intake.ti = FakeIntakeTi()
    assert [p["reason"] for p in intake.plan_runs()] == ["changed"]
    intake.ti = FakeIntakeTi(broken=True)
    intake.ti.get_dag = lambda dag_id: SimpleNamespace(dag_id=dag_id, is_paused=False)
    with pytest.raises(RuntimeError, match="API down"):  # Airflow cannot say: no guess that the run is dead
        intake.plan_runs()
