"""python -m px_runner <task> --company <co> --run-id <run_id>

<task>: aliases download finder records evidence indirect publish finalize (deploy/INTERFACES.md). Settings come
from the environment (PX_BUCKET, AWS_REGION, PX_SECRET_SEC, PX_SECRET_LLM, PX_LOCAL_ROOT, PX_REPO, PX_PYTHON,
PX_WORKERS, PX_NPORT_DB). Exit code 0: the task succeeded or was skipped; for finalize, the run's status
(succeeded or failed) is recorded. Non-zero: the task failed; for finalize, finalize itself failed (the company_evidence
DAG marks a failed run's DAG run failed from the task states, not from this exit code).
"""
from __future__ import annotations

import argparse
import re
import signal
import sys

from . import tasks


def _checked(pattern: str, what: str):
    rx = re.compile(pattern)

    def check(value: str) -> str:
        if not rx.fullmatch(value) or ".." in value:
            raise argparse.ArgumentTypeError(f"not a valid {what}: {value!r}")
        return value
    return check


def _terminate(signum, frame):
    raise tasks.Terminated(f"stopped by signal {signum} (task timeout or container stop)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m px_runner",
                                 description="Run one task of the company evidence pipeline against S3.")
    ap.add_argument("task", choices=tasks.TASKS)
    ap.add_argument("--company", required=True, type=_checked(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", "company"))
    ap.add_argument("--run-id", required=True, type=_checked(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,199}", "run id"))
    a = ap.parse_args(argv)
    settings = tasks.Settings.from_env()
    signal.signal(signal.SIGTERM, _terminate)
    return tasks.run(a.task, a.company, a.run_id, settings, tasks.client("s3", settings),
                     tasks.client("secretsmanager", settings))


if __name__ == "__main__":
    sys.exit(main())
