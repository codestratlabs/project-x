"""Shared test setup: the DAGs folder on sys.path, and a throwaway Airflow home for the integrity tests."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

DAGS_DIR = Path(__file__).resolve().parents[1]
if str(DAGS_DIR) not in sys.path:
    sys.path.insert(0, str(DAGS_DIR))

# Before anything imports airflow: no examples, and nothing written to ~/airflow.
os.environ.setdefault("AIRFLOW_HOME", tempfile.mkdtemp(prefix="px-airflow-home-"))
os.environ.setdefault("AIRFLOW__CORE__LOAD_EXAMPLES", "False")


@pytest.fixture
def clean_px_variables(monkeypatch):
    """No ``AIRFLOW_VAR_PX_*`` from the caller's environment: every Variable takes its default."""
    for name in list(os.environ):
        if name.upper().startswith("AIRFLOW_VAR_PX_"):
            monkeypatch.delenv(name)
    return monkeypatch


@pytest.fixture
def fake_aws(monkeypatch):
    """Fake credentials and no profile, so moto is the only AWS a test can reach."""
    for name in ("AWS_PROFILE", "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-2")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
