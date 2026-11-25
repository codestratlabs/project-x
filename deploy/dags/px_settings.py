"""Settings shared by the company_evidence and company_intake DAGs: Airflow Variables with defaults.

Every setting is an Airflow Variable with a default (``DEFAULTS``); a Variable that is not set, or set to an empty
string, takes its default. ``get()`` never needs the network or a database outside Airflow: without an Airflow
runtime (tests, a plain ``import``) it reads only ``AIRFLOW_VAR_<NAME>`` environment variables and falls back to
the default, so the DAG files import anywhere.

When each one is read:

- company_evidence reads its Variables when the file is parsed, because the backend (docker or ecs) picks the
  operator class and ``pool`` and ``queue`` are fixed at scheduling time. Airflow 3 parses the file again on the
  worker when a task starts, so a change reaches the next task that starts.
- company_intake reads its Variables when its tasks run (``px_bucket``, ``px_region``, ``px_refresh_days``,
  ``px_stale_hours``); the failure callbacks read ``px_alert_emails`` when they fire.

Variables from deploy/INTERFACES.md (section DAGs):

=========================  ==================================  ================================================
Variable                   Default                             Meaning
=========================  ==================================  ================================================
px_bucket                  company-reports-data                S3 bucket (PX_BUCKET in the task container)
px_region                  us-east-2                           AWS region (AWS_REGION in the task container)
px_image                   px-pipeline:latest                  image URI (ECR) for the docker backend; the ecs
                                                               backend runs the task definition's image
px_task_backend            docker                              ``docker`` (DockerOperator) or ``ecs``
                                                               (EcsRunTaskOperator)
px_docker_url              unix://var/run/docker.sock          Docker daemon of the docker backend
px_large_queue             evidence_large                      Celery queue of task ``evidence`` (docker backend)
px_queue                   (none: Airflow's default queue)     Celery queue of the other tasks (docker backend):
                                                               the px workers, whose role holds the task policy
px_ecs_cluster             (none; required for ecs)            ECS cluster
px_ecs_task_definition     (none; required for ecs)            task definition (family or family:revision); its
                                                               task role reads no secret
px_ecs_subnets             (none; required for ecs)            subnet ids: JSON list or comma-separated
px_ecs_security_groups     (none; required for ecs)            security group ids: JSON list or comma-separated
px_ecs_task_role_sec       (none; required for ecs)            ARN of the task role of ``download`` (SEC user
                                                               agent)
px_ecs_task_role_llm       (none; required for ecs)            ARN of the task role of ``finder`` and ``indirect``
                                                               (model key)
px_alert_emails            (none: no e-mail)                   failure e-mail recipients: JSON list or
                                                               comma-separated
px_refresh_days            7                                   re-run a company whose latest run ended more than
                                                               this many days ago
px_stale_hours             24                                  a queued/running record unchanged for longer than
                                                               this (and not active in Airflow) is a dead run
px_sec_pool                sec_api                             Airflow pool of task ``download``
px_secret_sec              px/sec-user-agent                   PX_SECRET_SEC in the task container
px_secret_llm              px/openrouter-api-key               PX_SECRET_LLM in the task container
=========================  ==================================  ================================================

Optional Variables the contract leaves open (defaults work with a task definition set up as described):

=========================  ==================================  ================================================
px_ecs_container           px                                  container name in the task definition (command
                                                               override; its awslogs-stream-prefix is ``ecs``)
px_ecs_log_group           /ecs/px-pipeline                    CloudWatch log group of that container; the task
                                                               log in Airflow shows it. ``none``: no log copy
px_ecs_large_overrides     {"cpu": "8192", "memory": "61440",  ECS RunTask task overrides for ``evidence`` (the
                           "ephemeralStorage":                 larger CPU/memory of the contract)
                           {"sizeInGiB": 200}}
px_task_env                {}                                  extra environment for every task container, JSON
                                                               object, non-secret values only (for example
                                                               PX_WORKERS, AWS_ENDPOINT_URL for a test S3)
px_task_timeout_hours      12                                  execution_timeout of each try of each task of
                                                               company_evidence: the container is stopped
px_run_timeout_hours       72                                  dagrun_timeout of company_evidence, counted from
                                                               the start of the DAG run (waits in a pool count)
px_large_slots             1                                   backend docker: evidence tasks running at once
                                                               across all runs (one per machine sized for it)
=========================  ==================================  ================================================

An invalid value (a backend other than docker or ecs, ECS settings missing, malformed JSON, a timeout that is not a
positive number) never stops a DAG file from importing; company_evidence then fails each task at once with the reason
(see its docs). A Variable that Airflow cannot read (an API or database error) is different: ``get()`` raises
``VariableUnreadable`` rather than using the default, so the parse or the task fails and is tried again. Airflow 3.2
answers "not set" for such an error too, so ``get()`` asks the API once more before it uses a default.

Failure e-mail goes through the SMTP provider (``SmtpNotifier``) on the connection named by ``[email]
email_conn_id`` (default ``smtp_default``), whose ``from_email`` is the sender. Secrets are never set here: the
task reads the SEC user agent and the model key from Secrets Manager itself (deploy/INTERFACES.md, Runner).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections.abc import Mapping
from typing import Any

log = logging.getLogger(__name__)

AWS_CONN_ID = "aws_default"
BACKENDS = ("docker", "ecs")

DEFAULTS: dict[str, str] = {
    "px_bucket": "company-reports-data",
    "px_region": "us-east-2",
    "px_image": "px-pipeline:latest",
    "px_task_backend": "docker",
    "px_docker_url": "unix://var/run/docker.sock",
    "px_large_queue": "evidence_large",
    "px_queue": "",
    "px_ecs_cluster": "",
    "px_ecs_task_definition": "",
    "px_ecs_subnets": "",
    "px_ecs_security_groups": "",
    "px_ecs_task_role_sec": "",
    "px_ecs_task_role_llm": "",
    "px_alert_emails": "",
    "px_refresh_days": "7",
    "px_stale_hours": "24",
    "px_sec_pool": "sec_api",
    "px_secret_sec": "px/sec-user-agent",
    "px_secret_llm": "px/openrouter-api-key",
    # Optional, not in the contract's list (see the module docstring).
    "px_ecs_container": "px",
    "px_ecs_log_group": "/ecs/px-pipeline",
    "px_ecs_large_overrides": '{"cpu": "8192", "memory": "61440", "ephemeralStorage": {"sizeInGiB": 200}}',
    "px_task_env": "{}",
    "px_task_timeout_hours": "12",
    "px_run_timeout_hours": "72",
    "px_large_slots": "1",
}

# The pipeline's secrets come from Secrets Manager inside the task, never from a Variable.
_FORBIDDEN_ENV = frozenset({"OPENROUTER_API_KEY", "SEC_USER_AGENT"})
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class VariableUnreadable(RuntimeError):
    """Airflow could not read a Variable (not the same as a Variable that is not set): its default is not used."""


def _supervisor_comms() -> Any:
    """The channel of a task or DAG-file parse to the Airflow API server; None outside an Airflow runtime."""
    try:
        from airflow.sdk.execution_time import task_runner
    except ImportError:
        return None
    return getattr(task_runner, "SUPERVISOR_COMMS", None)


def _confirm_not_set(key: str) -> str | None:
    """``Variable.get`` reported ``key`` as not set, which Airflow 3.2's API backend also reports when the call failed.
    Ask the API server once more: None when the Variable does not exist, its value when it does, and
    ``VariableUnreadable`` when the call fails. Outside an Airflow runtime there is no one to ask: None."""
    comms = _supervisor_comms()
    if comms is None:
        return None
    try:
        from airflow.sdk.exceptions import ErrorType
        from airflow.sdk.execution_time.comms import ErrorResponse, GetVariable, VariableResult
    except ImportError:  # pragma: no cover - another Airflow version: no second check
        return None
    try:
        reply = comms.send(GetVariable(key=key))
    except Exception as exc:
        raise VariableUnreadable(f"cannot read Airflow Variable {key} ({type(exc).__name__}: {exc})") from exc
    if isinstance(reply, VariableResult):
        return reply.value
    if isinstance(reply, ErrorResponse) and reply.error == ErrorType.VARIABLE_NOT_FOUND:
        return None
    raise VariableUnreadable(f"cannot read Airflow Variable {key} ({getattr(reply, 'error', reply)!s})")


def get(key: str) -> str:
    """The Variable ``key`` as a stripped string, or its default when it is not set or empty.

    Raises ``VariableUnreadable`` when Airflow cannot read it: running a task with a default in place of the value
    that is set (another backend, image, bucket or task environment) would be wrong without any error.
    """
    default = DEFAULTS[key]
    try:
        from airflow.sdk import Variable

        value = Variable.get(key, default=None)
    except Exception as exc:
        if _supervisor_comms() is not None:
            raise VariableUnreadable(f"cannot read Airflow Variable {key} ({type(exc).__name__}: {exc})") from exc
        # no Airflow runtime at all (tests, a plain import): the DAG must still import
        log.warning("px_settings: cannot read Variable %s (%s); using its default", key, type(exc).__name__)
        return default
    if value is None:
        value = _confirm_not_set(key)
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def parse_list(text: str) -> list[str]:
    """A JSON list, or items separated by commas, semicolons or white space."""
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        items = json.loads(text)
        if not isinstance(items, list):
            raise ValueError(f"expected a JSON list, got {type(items).__name__}")
        return [str(item).strip() for item in items if str(item).strip()]
    return [item for item in re.split(r"[,;\s]+", text) if item]


def get_list(key: str) -> list[str]:
    try:
        return parse_list(get(key))
    except ValueError as exc:
        raise ValueError(f"Variable {key}: {exc}") from exc


def get_json_object(key: str) -> dict[str, Any]:
    try:
        value = json.loads(get(key))
    except ValueError as exc:
        raise ValueError(f"Variable {key} must be a JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Variable {key} must be a JSON object, got {type(value).__name__}")
    return value


def get_positive_number(key: str) -> float:
    text = get(key)
    try:
        number = float(text)
    except ValueError:
        raise ValueError(f"Variable {key} must be a number, got {text!r}") from None
    if not number > 0 or number == float("inf"):
        raise ValueError(f"Variable {key} must be greater than 0, got {text!r}")
    return number


def task_backend() -> str:
    """``px_task_backend``: ``docker`` (default) or ``ecs``. Any other value is a configuration error."""
    backend = get("px_task_backend").lower()
    if backend not in BACKENDS:
        raise ValueError(f"Variable px_task_backend must be one of {', '.join(BACKENDS)}; got {backend!r}")
    return backend


def task_environment() -> dict[str, str]:
    """The environment of every task container: the PX_* settings of deploy/INTERFACES.md (Runner).

    ``px_task_env`` adds non-secret extras; the settings below always win over it.
    """
    env: dict[str, str] = {}
    for name, value in get_json_object("px_task_env").items():
        if not _ENV_NAME_RE.match(str(name)):
            raise ValueError(f"Variable px_task_env: {name!r} is not an environment variable name")
        if str(name).upper() in _FORBIDDEN_ENV:
            raise ValueError(f"Variable px_task_env: {name} is a secret; the task reads it from Secrets Manager")
        env[str(name)] = "" if value is None else str(value)
    region = get("px_region")
    env.update(
        {
            "PX_BUCKET": get("px_bucket"),
            "AWS_REGION": region,
            "AWS_DEFAULT_REGION": region,
            "PX_SECRET_SEC": get("px_secret_sec"),
            "PX_SECRET_LLM": get("px_secret_llm"),
        }
    )
    return env


# ---- failure e-mail ----------------------------------------------------------------------------------------------

try:
    from airflow.sdk.exceptions import AirflowFailException
except ImportError:  # pragma: no cover - Airflow 2
    from airflow.exceptions import AirflowFailException


class RunFailed(AirflowFailException):
    """Raised by ``finalize`` when a pipeline task did not succeed, so the DAG run ends ``failed``.

    The failed task has sent its own e-mail, so ``alert_on_failure`` sends none for this one. (The runner's finalize
    exits 0 once it has recorded the run as failed, so ``finalize`` fails through this exception only, and an e-mail
    from ``finalize`` means that finalize itself did not work.) Raise it through
    ``raise_run_failed``, which also marks the context: Airflow does not always put the exception in the callback's
    context (its in-process runner, used by ``airflow dags test``, leaves it out).
    """


RUN_FAILED_KEY = "px_run_failed"


def raise_run_failed(context: Any, message: str) -> None:
    """Mark the task context (seen by the failure callback) and raise ``RunFailed``."""
    with contextlib.suppress(TypeError):  # a read-only mapping: the exception type alone tells the callback
        context[RUN_FAILED_KEY] = True
    raise RunFailed(message)


# Jinja, rendered by SmtpNotifier with the task context (Airflow's StrictUndefined: only keys that always exist,
# so the error text is passed in as px_error rather than read from the context's optional "exception").
_SUBJECT = "[px] {{ ti.dag_id }}: {{ ti.task_id }} failed{{ px_for }} ({{ run_id }})"
_BODY = """\
<p>Task <b>{{ ti.task_id | e }}</b> of DAG <b>{{ ti.dag_id | e }}</b> failed (try {{ ti.try_number }}).</p>
<p>Company: {{ px_company | e }}<br>
Run: {{ px_run_id | e }}<br>
Run record: {{ px_record | e }}</p>
<p>Error:</p>
<pre>{{ px_error | truncate(4000) | e }}</pre>
<p><a href="{{ ti.log_url }}">Task log</a></p>
"""


def alert_on_failure(context: Mapping[str, Any]) -> None:
    """``on_failure_callback``: e-mail the failure to ``px_alert_emails`` (nothing when it is empty).

    Never raises: a failed e-mail is logged and does not change the task's state.
    """
    try:
        error = context.get("exception")
        if context.get(RUN_FAILED_KEY) or isinstance(error, RunFailed):
            return
        recipients = get_list("px_alert_emails")
        if not recipients:
            log.info("px: px_alert_emails is empty; no failure e-mail")
            return
        from airflow.providers.smtp.notifications.smtp import SmtpNotifier

        params = context.get("params") or {}
        # company_evidence has the company in its params; company_intake's mapped tasks name it in the map index.
        company = params.get("company") or getattr(context.get("ti"), "rendered_map_index", None)
        run_id = params.get("run_id")
        extra = {
            "px_for": f" for {company}" if company else "",
            "px_company": company or "-",
            "px_run_id": run_id or context.get("run_id") or "-",
            "px_record": f"s3://{get('px_bucket')}/runs/{company}/{run_id}.json" if company and run_id else "-",
            "px_error": str(error) if error is not None else "(not given to the callback; see the task log)",
        }
        notifier = SmtpNotifier(to=recipients, subject=_SUBJECT, html_content=_BODY)
        notifier({**context, **extra})
    except Exception:
        log.exception("px: the failure e-mail was not sent")


# A DAG run's failure callback runs in the DAG processor. Its context is either a task context of the last failed or
# unfinished task plus "reason", or only {"dag", "run_id", "reason"}: the template uses only values set here.
_RUN_SUBJECT = "[px] {{ px_dag_id }}: run failed{{ px_for }}, {{ px_reason }} ({{ px_run_id }})"
_RUN_BODY = """\
<p>Run <b>{{ px_run_id | e }}</b> of DAG <b>{{ px_dag_id | e }}</b> failed: {{ px_reason | e }}.</p>
<p>Company: {{ px_company | e }}<br>
Run record: {{ px_record | e }}<br>
Last task: {{ px_task | e }}</p>
<p>{{ px_hint | e }}</p>
"""
_RUN_HINTS = {
    ("company_evidence", "timed_out"): "The run took longer than px_run_timeout_hours: its unfinished tasks were set "
    "to skipped and finalize did not run, so the run record stays running until px_stale_hours have passed and "
    "company_intake starts the company again. Check the task logs for the task that hung, or a pool that does not "
    "exist. Backend docker: the stopped task's container can stay on its worker, exited, with the files it staged, "
    "until the company's next run there removes it (docker ps -a --filter name=px- --filter status=exited).",
}


def _alert_on_run_failure(context: Mapping[str, Any]) -> None:
    try:
        recipients = get_list("px_alert_emails")
        if not recipients:
            log.info("px: px_alert_emails is empty; no failure e-mail")
            return
        from airflow.providers.smtp.notifications.smtp import SmtpNotifier

        dag, dag_run, ti = context.get("dag"), context.get("dag_run"), context.get("ti")
        conf = getattr(dag_run, "conf", None) or {}
        # company_evidence: the company is in the conf. company_intake's runs have no conf, and the task instance that
        # Airflow 3.2 gives a DAG callback has the map index of a failed start_run task but not the company rendered
        # from it, so the e-mail gives the map index: the grid view shows the company of each.
        company, run_id = conf.get("company"), conf.get("run_id")
        task, map_index = getattr(ti, "task_id", None) or "-", getattr(ti, "map_index", None)
        if isinstance(map_index, int) and map_index >= 0:
            task += f", map index {map_index}"
        reason = str(context.get("reason") or "unknown reason")
        dag_id = getattr(dag, "dag_id", None) or getattr(dag_run, "dag_id", None) or "-"
        extra = {
            "px_dag_id": dag_id,
            "px_run_id": context.get("run_id") or getattr(dag_run, "run_id", None) or "-",
            "px_reason": reason,
            "px_for": f" for {company}" if company else "",
            "px_company": company or "-",
            "px_record": f"s3://{get('px_bucket')}/runs/{company}/{run_id}.json" if company and run_id else "-",
            "px_task": task,
            "px_hint": _RUN_HINTS.get((dag_id, reason), "See the task logs of this run in Airflow."),
        }
        notifier = SmtpNotifier(to=recipients, subject=_RUN_SUBJECT, html_content=_RUN_BODY)
        # rendered from these values alone: the context's "dag" can be a SerializedDAG, which has no template env
        notifier(dict(extra))
    except Exception:
        log.exception("px: the failure e-mail was not sent")


def alert_on_run_failure(context: Mapping[str, Any]) -> None:
    """DAG ``on_failure_callback`` of company_intake: e-mail every failed DAG run. Its tasks have no e-mail of their
    own: a task can die without calling its callback (in Airflow 3.2.2, TriggerDagRunOperator whose API call fails
    with anything but 409 crashes the task runner before the callback), and the DAG processor still runs this one."""
    _alert_on_run_failure(context)


def alert_on_run_timeout(context: Mapping[str, Any]) -> None:
    """DAG ``on_failure_callback`` of company_evidence: e-mail a DAG run that failed without a failed task, which is a
    run past its ``dagrun_timeout`` (a failed task has sent its own e-mail; ``reason`` is then ``task_failure``)."""
    if context.get("reason") == "task_failure":
        return
    _alert_on_run_failure(context)
