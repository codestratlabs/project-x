"""Checks of deploy/iam/ and of the input-file example in deploy/README.md.

    python -m pip install pytest boto3 "moto[iam,s3]"
    python -m pytest deploy/iam -q

No AWS account is used: moto stands in for IAM and S3, with fake credentials and fake ARNs. moto checks a policy's
actions and resources, not its conditions (s3:prefix, s3:ResourceAccount), so the conditions are checked on the
policy text.
"""
from __future__ import annotations

import ast
import csv
import json
import os
import re
import string
import subprocess
import sys
from pathlib import Path

import pytest

boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")
from botocore.exceptions import ClientError  # noqa: E402
from moto import mock_aws  # noqa: E402
from moto.core import set_initial_no_auth_action_count  # noqa: E402
from moto.iam.access_control import IAMPolicy, PermissionResult  # noqa: E402

HERE = Path(__file__).resolve().parent
DEPLOY = HERE.parent
REPO = DEPLOY.parent

ACCOUNT = "123456789012"                       # moto's account
REGION = "us-east-2"
BUCKET = "company-reports-data"
BUCKET_ARN = f"arn:aws:s3:::{BUCKET}"
SEC_ARN = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:px/sec-user-agent-AbCdEf"
LLM_ARN = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:px/openrouter-api-key-GhIjKl"
FAKE = {"BUCKET": BUCKET, "ACCOUNT_ID": ACCOUNT, "REGION": REGION, "SECRET_SEC_ARN": SEC_ARN, "SECRET_LLM_ARN": LLM_ARN,
        "IMAGE_URI": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/px-pipeline:0123456789ab", "ECS_CLUSTER": "",
        "ECS_TASK_DEFINITION": "", "ECS_SUBNETS": "", "ECS_SECURITY_GROUPS": "", "ECS_TASK_ROLE_SEC": "",
        "ECS_TASK_ROLE_LLM": "", "PX_QUEUE": "px", "ALERT_EMAILS": "ops@example.com"}

# every JSON file of the folder and the placeholders it may hold
PLACEHOLDERS = {
    "task-role-policy.json": {"BUCKET", "ACCOUNT_ID"},
    "task-secret-sec-policy.json": {"SECRET_SEC_ARN"},
    "task-secret-llm-policy.json": {"SECRET_LLM_ARN"},
    "intake-role-policy.json": {"BUCKET", "ACCOUNT_ID"},
    "task-role-trust-ecs.json": {"ACCOUNT_ID", "REGION"},
    "airflow-variables.example.json": {"IMAGE_URI", "PX_QUEUE", "ECS_CLUSTER", "ECS_TASK_DEFINITION", "ECS_SUBNETS",
                                       "ECS_SECURITY_GROUPS", "ECS_TASK_ROLE_SEC", "ECS_TASK_ROLE_LLM", "ALERT_EMAILS"},
    "airflow-pools.example.json": set(),
}
POLICIES = ("task-role-policy.json", "task-secret-sec-policy.json", "task-secret-llm-policy.json",
            "intake-role-policy.json")
# The task roles (deploy/README.md section 5): every one has the task policy; download's adds the SEC user agent, the
# role of finder and indirect the model key. Backend ecs runs each task as its role; on docker the px workers' role
# has all three policies.
ROLES = {"px-task": ("task-role-policy.json",),
         "px-task-sec": ("task-role-policy.json", "task-secret-sec-policy.json"),
         "px-task-llm": ("task-role-policy.json", "task-secret-llm-policy.json")}
ROLE_OF_TASK = {"aliases": "px-task", "download": "px-task-sec", "finder": "px-task-llm", "records": "px-task",
                "evidence": "px-task", "indirect": "px-task-llm", "publish": "px-task", "finalize": "px-task"}
RUN = "20261117T101500Z-1a2b3c4d"


def text(name: str) -> str:
    return (HERE / name).read_text(encoding="utf-8")


def strict_json(s: str):
    """json.loads that also rejects a key given twice (json keeps the last one silently)."""
    def pairs(items):
        keys = [k for k, _ in items]
        assert len(keys) == len(set(keys)), f"duplicate keys: {sorted(k for k in keys if keys.count(k) > 1)}"
        return dict(items)
    return json.loads(s, object_pairs_hook=pairs)


def filled(name: str) -> str:
    return string.Template(text(name)).substitute(FAKE)


def obj(key: str, bucket: str = BUCKET) -> str:
    return f"arn:aws:s3:::{bucket}/{key}"


def allows(policy: str, action: str, resource: str) -> bool:
    return IAMPolicy(policy).is_action_permitted(action, resource) == PermissionResult.PERMITTED


def string_like(pattern: str, value: str) -> bool:
    """IAM StringLike: * is any run of characters (including /), ? is one character."""
    rx = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return re.fullmatch(rx, value, re.S) is not None


# ---------------------------------------------------------------- files and placeholders
def test_every_json_file_parses():
    names = sorted(p.name for p in HERE.glob("*.json"))
    assert names == sorted(PLACEHOLDERS), "a JSON file without a check here, or one missing"
    for name in names:
        assert isinstance(strict_json(text(name)), dict), name


def test_placeholders_are_documented_and_fill_to_valid_json():
    readme = text("README.md")
    for name, expected in PLACEHOLDERS.items():
        raw = text(name)
        found = set(re.findall(r"\$\{([A-Z_]+)\}", raw))
        assert found == expected, f"{name}: placeholders {sorted(found)}"
        assert raw.count("$") == len(re.findall(r"\$\{[A-Z_]+\}", raw)), f"{name}: a '$' outside ${{NAME}}"
        assert "${" not in json.dumps(strict_json(filled(name))), name
        for p in expected:
            assert f"${{{p}}}" in readme, f"{p} is not explained in deploy/iam/README.md"


# ---------------------------------------------------------------- IAM documents
@mock_aws
def test_filled_policies_are_valid_iam_documents():
    iam = boto3.client("iam", region_name=REGION)
    with pytest.raises(ClientError, match="MalformedPolicyDocument"):      # the validator is active
        iam.create_policy(PolicyName="bad", PolicyDocument=json.dumps(
            {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "bucket/key"}]}))
    for name in POLICIES:
        iam.create_policy(PolicyName=name.removesuffix(".json"), PolicyDocument=filled(name))
    iam.create_role(RoleName="px-task", AssumeRolePolicyDocument=filled("task-role-trust-ecs.json"))
    iam.update_assume_role_policy(RoleName="px-task", PolicyDocument=filled("task-role-trust-ecs.json"))


def test_statements_allow_only_named_actions_and_pin_the_bucket_account():
    for name in POLICIES:
        for st in strict_json(filled(name))["Statement"]:
            actions = [st["Action"]] if isinstance(st["Action"], str) else st["Action"]
            assert st["Effect"] == "Allow" and "NotAction" not in st and "NotResource" not in st, st["Sid"]
            assert not any("*" in a for a in actions), f"{name} {st['Sid']}: wildcard action"
            resources = [st["Resource"]] if isinstance(st["Resource"], str) else st["Resource"]
            assert "*" not in resources, f"{name} {st['Sid']}: Resource *"
            if any(a.startswith("s3:") for a in actions):
                assert st["Condition"]["StringEquals"]["s3:ResourceAccount"] == ACCOUNT, f"{name} {st['Sid']}"


def test_trust_policy_admits_only_ecs_tasks_of_the_account():
    (st,) = strict_json(filled("task-role-trust-ecs.json"))["Statement"]
    assert st["Principal"] == {"Service": "ecs-tasks.amazonaws.com"} and st["Action"] == "sts:AssumeRole"
    assert st["Condition"]["StringEquals"]["aws:SourceAccount"] == ACCOUNT
    assert st["Condition"]["ArnLike"]["aws:SourceArn"] == f"arn:aws:ecs:{REGION}:{ACCOUNT}:*"


# ---------------------------------------------------------------- what each role may do
TASK_ALLOWED = [
    ("s3:ListBucket", BUCKET_ARN),
    ("s3:GetObject", obj("input/lungpacer/company.json")),
    ("s3:GetObject", obj("reference/formd/formd.duckdb")),
    ("s3:GetObject", obj("reference/nport/lungpacer/nport_holdings.csv")),
    ("s3:GetObject", obj("reference/dol/raw/form5500_rows.csv")),
    ("s3:GetObject", obj("raw/lungpacer/sec/manifest.csv")),
    ("s3:PutObject", obj("raw/lungpacer/sec/manifest.csv")),
    ("s3:PutObject", obj("raw/lungpacer/sec/files/10-K/0001708893-21-000002_primary_doc.xml")),
    ("s3:AbortMultipartUpload", obj("raw/lungpacer/sec/files/S-1/big.htm")),
    ("s3:GetObject", obj(f"intermediate/lungpacer/{RUN}/mentions/out/lungpacer/lungpacer_mentions.csv")),
    ("s3:PutObject", obj(f"intermediate/lungpacer/{RUN}/evidence/evidence_rows.csv")),
    ("s3:PutObject", obj(f"intermediate/lungpacer/{RUN}/publish/indirect_located.csv")),
    ("s3:PutObject", obj(f"reports/lungpacer/{RUN}/Lungpacer_Medical_findings.xlsx")),
    ("s3:PutObject", obj("reports/lungpacer/latest/Lungpacer_Medical_findings.xlsx")),
    ("s3:DeleteObject", obj("reports/lungpacer/latest/Lungpacer_findings.xlsx")),
    # a re-run task removes what its earlier attempt left in its own folders of the run
    ("s3:DeleteObject", obj(f"intermediate/lungpacer/{RUN}/prepared/pdfs/0001708893-21-000002_a.htm")),
    ("s3:GetObject", obj(f"runs/lungpacer/{RUN}.json")),
    ("s3:PutObject", obj(f"runs/lungpacer/{RUN}.json")),
]
TASK_DENIED = [
    ("secretsmanager:GetSecretValue", SEC_ARN),                                    # only in the secret policies
    ("secretsmanager:GetSecretValue", LLM_ARN),
    ("s3:PutObject", obj("input/lungpacer/company.json")),                        # the analyst's file
    ("s3:DeleteObject", obj("input/lungpacer/company.json")),
    ("s3:GetObject", obj("input/lungpacer/notes.txt")),
    ("s3:PutObject", obj("reference/formd/formd.duckdb")),                         # shared, read-only
    ("s3:DeleteObject", obj("reference/nport/lungpacer/nport_holdings.csv")),
    ("s3:DeleteObject", obj("raw/lungpacer/sec/manifest.csv")),                    # filings are kept across runs
    ("s3:DeleteObject", obj("raw/lungpacer/sec/files/10-K/x.htm")),
    ("s3:DeleteObject", obj(f"reports/lungpacer/{RUN}/Lungpacer_Medical_findings.xlsx")),   # per-run workbooks are kept
    ("s3:DeleteObject", obj(f"runs/lungpacer/{RUN}.json")),
    ("s3:PutObject", obj("runs/lungpacer/notes.txt")),
    ("s3:GetObject", obj("other/file")),
    ("s3:PutObject", obj("other/file")),
    ("s3:PutBucketPolicy", BUCKET_ARN),
    ("s3:DeleteBucket", BUCKET_ARN),
    ("s3:PutObjectAcl", obj("raw/lungpacer/sec/manifest.csv")),
    ("s3:ListBucket", "arn:aws:s3:::another-bucket"),
    ("s3:GetObject", obj("input/lungpacer/company.json", "another-bucket")),
    ("secretsmanager:GetSecretValue", f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:another-secret-AbCdEf"),
    ("secretsmanager:PutSecretValue", SEC_ARN),
    ("secretsmanager:DescribeSecret", LLM_ARN),
    ("secretsmanager:ListSecrets", "*"),
]
INTAKE_ALLOWED = [
    ("s3:ListBucket", BUCKET_ARN),
    ("s3:GetObject", obj(f"runs/lungpacer/{RUN}.json")),
    ("s3:PutObject", obj(f"runs/lungpacer/{RUN}.json")),
]
INTAKE_DENIED = [
    ("s3:GetObject", obj("input/lungpacer/company.json")),                        # the listing's ETag is enough
    ("s3:PutObject", obj("input/lungpacer/company.json")),
    ("s3:DeleteObject", obj("input/lungpacer/company.json")),
    ("s3:DeleteObject", obj(f"runs/lungpacer/{RUN}.json")),
    ("s3:PutObject", obj("runs/lungpacer/notes.txt")),
    ("s3:GetObject", obj("raw/lungpacer/sec/manifest.csv")),
    ("s3:GetObject", obj(f"intermediate/lungpacer/{RUN}/mentions/out/lungpacer/lungpacer_mentions.csv")),
    ("s3:GetObject", obj("reports/lungpacer/latest/Lungpacer_Medical_findings.xlsx")),
    ("s3:GetObject", obj("reference/formd/formd.duckdb")),
    ("s3:PutObject", obj(f"reports/lungpacer/{RUN}/x.xlsx")),
    ("secretsmanager:GetSecretValue", SEC_ARN),
    ("secretsmanager:GetSecretValue", LLM_ARN),
]


SECRET_DENIED = [
    ("secretsmanager:GetSecretValue", f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:another-secret-AbCdEf"),
    ("secretsmanager:PutSecretValue", SEC_ARN),
    ("secretsmanager:PutSecretValue", LLM_ARN),
    ("secretsmanager:DescribeSecret", SEC_ARN),
    ("secretsmanager:ListSecrets", "*"),
    ("s3:GetObject", obj("input/lungpacer/company.json")),
    ("s3:ListBucket", BUCKET_ARN),
]


@pytest.mark.parametrize("name, allowed, denied", [
    ("task-role-policy.json", TASK_ALLOWED, TASK_DENIED),
    ("task-secret-sec-policy.json", [("secretsmanager:GetSecretValue", SEC_ARN)],
     [("secretsmanager:GetSecretValue", LLM_ARN), *SECRET_DENIED]),
    ("task-secret-llm-policy.json", [("secretsmanager:GetSecretValue", LLM_ARN)],
     [("secretsmanager:GetSecretValue", SEC_ARN), *SECRET_DENIED]),
    ("intake-role-policy.json", INTAKE_ALLOWED, INTAKE_DENIED),
])
def test_role_decisions(name, allowed, denied):
    policy = filled(name)
    assert [x for x in allowed if not allows(policy, *x)] == [], "needed but not allowed"
    assert [x for x in denied if allows(policy, *x)] == [], "allowed but not needed"


def test_each_task_role_reads_only_its_tasks_secret():
    """ARCHITECTURE's access model: only task 2 reads the SEC user agent, only tasks 3 and 6 the model key; every task
    role has the same S3 access (not limited per company: deploy/iam/README.md)."""
    secret_of_task = {"download": SEC_ARN, "finder": LLM_ARN, "indirect": LLM_ARN}
    for task, role in ROLE_OF_TASK.items():
        policies = [filled(name) for name in ROLES[role]]
        reads = {arn for arn in (SEC_ARN, LLM_ARN) if any(allows(p, "secretsmanager:GetSecretValue", arn) for p in policies)}
        assert reads == ({secret_of_task[task]} if task in secret_of_task else set()), task
        assert all(any(allows(p, *x) for p in policies) for x in TASK_ALLOWED), task


def test_intake_lists_only_input_and_runs():
    (st,) = [s for s in strict_json(filled("intake-role-policy.json"))["Statement"] if s["Action"] == "s3:ListBucket"]
    patterns = st["Condition"]["StringLike"]["s3:prefix"]
    may_list = lambda prefix: any(string_like(p, prefix) for p in patterns)     # noqa: E731
    assert all(map(may_list, ["input/", "input/lungpacer/", "runs/", "runs/lungpacer/"]))
    assert not any(map(may_list, ["", "raw/", "raw/lungpacer/sec/", "intermediate/", "reports/", "reference/", "inputs/", "runs"]))


# ---------------------------------------------------------------- the task wrapper's S3 calls, signed and checked
def _setup(tmp_path):
    """Three unchecked calls make an admin; everything after is signed and checked against the caller's policies."""
    iam = boto3.client("iam", region_name=REGION)
    iam.create_user(UserName="admin")
    iam.put_user_policy(UserName="admin", PolicyName="all", PolicyDocument=json.dumps(
        {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}))
    k = iam.create_access_key(UserName="admin")["AccessKey"]
    admin = boto3.Session(aws_access_key_id=k["AccessKeyId"], aws_secret_access_key=k["SecretAccessKey"], region_name=REGION)
    s3 = admin.client("s3")
    for b in (BUCKET, "another-bucket"):
        s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": REGION})
    for key in ("input/lungpacer/company.json", "reference/formd/formd.duckdb", "raw/lungpacer/sec/manifest.csv",
                "raw/lungpacer/sec/files/D/0001708893-26-000005_primary_doc.xml", f"runs/lungpacer/{RUN}.json",
                "reports/lungpacer/latest/Lungpacer_findings.xlsx"):
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"{}")
    s3.put_object(Bucket="another-bucket", Key="x", Body=b"x")

    def user(name: str, policy: str):
        a = admin.client("iam")
        a.create_user(UserName=name)
        a.put_user_policy(UserName=name, PolicyName=name, PolicyDocument=policy)
        k = a.create_access_key(UserName=name)["AccessKey"]
        return boto3.Session(aws_access_key_id=k["AccessKeyId"], aws_secret_access_key=k["SecretAccessKey"],
                             region_name=REGION).client("s3")
    return user


def _denied(call, **kw):
    with pytest.raises(ClientError) as e:
        call(**kw)
    assert e.value.response["Error"]["Code"] == "AccessDenied", e.value.response["Error"]


@set_initial_no_auth_action_count(3)
@mock_aws
def test_task_wrapper_s3_calls_with_task_policy(tmp_path):
    s3 = _setup(tmp_path)("task", filled("task-role-policy.json"))
    b = {"Bucket": BUCKET}
    # stage: input file, raw filings, reference data
    s3.get_object(**b, Key="input/lungpacer/company.json")
    keys = [o["Key"] for o in s3.list_objects_v2(**b, Prefix="raw/lungpacer/sec/")["Contents"]]
    assert "raw/lungpacer/sec/manifest.csv" in keys
    s3.download_file(BUCKET, "raw/lungpacer/sec/manifest.csv", str(tmp_path / "manifest.csv"))
    s3.download_file(BUCKET, "reference/formd/formd.duckdb", str(tmp_path / "formd.duckdb"))
    assert s3.list_objects_v2(**b, Prefix="reference/nport/lungpacer/")["KeyCount"] == 0      # optional file absent
    with pytest.raises(ClientError) as e:                                                     # a missing key is 404
        s3.head_object(**b, Key="reference/dol/data/welfare_plans_by_year.csv")
    assert e.value.response["Error"]["Code"] == "404"
    # upload: a large filing (multipart), the run's files, the workbook, the run record
    big = tmp_path / "big.htm"
    big.write_bytes(b"x" * (9 * 1024 * 1024))                                                # over boto3's 8 MB multipart threshold
    s3.upload_file(str(big), BUCKET, "raw/lungpacer/sec/files/S-1/big.htm")
    s3.put_object(**b, Key=f"intermediate/lungpacer/{RUN}/evidence/evidence_rows.csv", Body=b"a,b\n")
    s3.put_object(**b, Key=f"reports/lungpacer/{RUN}/Lungpacer_Medical_findings.xlsx", Body=b"x")
    s3.put_object(**b, Key="reports/lungpacer/latest/Lungpacer_Medical_findings.xlsx", Body=b"x")
    # publish replaces latest/ with delete_objects, which AWS authorizes per key as s3:DeleteObject; moto checks that
    # call against the bucket ARN instead, so the same permission is exercised here with delete_object.
    s3.delete_object(**b, Key="reports/lungpacer/latest/Lungpacer_findings.xlsx")
    s3.delete_object(**b, Key=f"intermediate/lungpacer/{RUN}/evidence/evidence_rows.csv")   # an earlier attempt's file
    record = json.loads(s3.get_object(**b, Key=f"runs/lungpacer/{RUN}.json")["Body"].read())
    s3.put_object(**b, Key=f"runs/lungpacer/{RUN}.json", Body=json.dumps({**record, "status": "running"}).encode())
    # never
    _denied(s3.put_object, **b, Key="input/lungpacer/company.json", Body=b"{}")
    _denied(s3.put_object, **b, Key="reference/formd/formd.duckdb", Body=b"x")
    _denied(s3.delete_object, **b, Key="raw/lungpacer/sec/manifest.csv")
    _denied(s3.delete_object, **b, Key=f"reports/lungpacer/{RUN}/Lungpacer_Medical_findings.xlsx")
    _denied(s3.delete_object, **b, Key=f"runs/lungpacer/{RUN}.json")
    _denied(s3.list_objects_v2, Bucket="another-bucket")
    _denied(s3.get_object, Bucket="another-bucket", Key="x")


@set_initial_no_auth_action_count(3)
@mock_aws
def test_intake_s3_calls_with_intake_policy(tmp_path):
    s3 = _setup(tmp_path)("intake", filled("intake-role-policy.json"))
    b = {"Bucket": BUCKET}
    inputs = s3.list_objects_v2(**b, Prefix="input/")["Contents"]
    assert [(o["Key"], bool(o["ETag"])) for o in inputs] == [("input/lungpacer/company.json", True)]
    assert [o["Key"] for o in s3.list_objects_v2(**b, Prefix="runs/lungpacer/")["Contents"]] == [f"runs/lungpacer/{RUN}.json"]
    s3.get_object(**b, Key=f"runs/lungpacer/{RUN}.json")
    s3.put_object(**b, Key="runs/lungpacer/20261124T101500Z-1a2b3c4d.json", Body=b'{"status": "queued"}',
                  IfNoneMatch="*")                                                 # the queued record, if absent
    _denied(s3.get_object, **b, Key="input/lungpacer/company.json")
    _denied(s3.put_object, **b, Key="input/lungpacer/company.json", Body=b"{}")
    _denied(s3.delete_object, **b, Key=f"runs/lungpacer/{RUN}.json")
    _denied(s3.get_object, **b, Key="raw/lungpacer/sec/manifest.csv")
    _denied(s3.get_object, **b, Key="reference/formd/formd.duckdb")
    _denied(s3.put_object, **b, Key="raw/lungpacer/sec/manifest.csv", Body=b"x")


# ---------------------------------------------------------------- Airflow examples against the contract
def _contract_variables() -> dict:
    """{name: default or None} from the px_settings.py bullet of deploy/INTERFACES.md."""
    doc = (DEPLOY / "INTERFACES.md").read_text(encoding="utf-8")
    bullet = doc[doc.index("- `deploy/dags/px_settings.py`"):doc.index("- `company_evidence`")]
    names = re.findall(r"`(px_[a-z_]+)`", bullet)
    out = {}
    for i, n in enumerate(names):
        start = bullet.index(f"`{n}`") + len(n) + 2
        end = bullet.index(f"`{names[i + 1]}`") if i + 1 < len(names) else len(bullet)
        tail = bullet[start:end]
        d = re.search(r"default `([^`]+)`", tail) or re.match(r"\s*\(`([^`]+)`\)", tail)
        out[n] = d[1] if d else None
    return out


def _runner_default(env: str) -> str:
    doc = (DEPLOY / "INTERFACES.md").read_text(encoding="utf-8")
    return re.search(rf"^\| `{env}` \| `([^`]+)` \|", doc, re.M)[1]


def test_airflow_variables_cover_the_contract():
    contract = _contract_variables()
    assert len(contract) == 19
    example = strict_json(text("airflow-variables.example.json"))
    assert set(example) == set(contract)
    assert all(isinstance(v, str) for v in example.values()), "Airflow Variables are strings"
    for name, default in contract.items():
        if default is not None:
            assert example[name] == default, name
    assert example["px_secret_sec"] == _runner_default("PX_SECRET_SEC")
    assert example["px_secret_llm"] == _runner_default("PX_SECRET_LLM")
    assert example["px_bucket"] == _runner_default("PX_BUCKET") and example["px_region"] == _runner_default("AWS_REGION")


def test_airflow_variables_match_the_dag_settings():
    """The example's names exist in deploy/dags/px_settings.py and its fixed values are that file's defaults."""
    path = DEPLOY / "dags" / "px_settings.py"
    if not path.exists():
        pytest.skip("deploy/dags/px_settings.py is not there")
    defaults = None
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        targets = [node.target] if isinstance(node, ast.AnnAssign) else getattr(node, "targets", [])
        if any(getattr(t, "id", "") == "DEFAULTS" for t in targets) and node.value is not None:
            defaults = ast.literal_eval(node.value)
    assert isinstance(defaults, dict), "no DEFAULTS dict in px_settings.py"
    example = strict_json(text("airflow-variables.example.json"))
    assert set(example) <= set(defaults), sorted(set(example) - set(defaults))
    for name, value in example.items():
        if not value.startswith("${"):
            assert value == defaults[name], name


def test_pool_example():
    pools = strict_json(text("airflow-pools.example.json"))
    assert list(pools) == [strict_json(text("airflow-variables.example.json"))["px_sec_pool"]] == ["sec_api"]
    p = pools["sec_api"]
    assert p["slots"] == 1
    assert isinstance(p["description"], str) and p["description"]    # `airflow pools import` needs slots and description
    assert p.get("include_deferred", False) is False


# ---------------------------------------------------------------- the input example of deploy/README.md
def test_readme_input_example_matches_the_config_and_feeds_make_companies_file(tmp_path):
    guide = (DEPLOY / "README.md").read_text(encoding="utf-8")
    blocks = [b for b in re.findall(r"```json\n(.*?)```", guide, re.S) if '"run_indirect"' in b]
    assert len(blocks) == 1, "one JSON example with run_indirect in deploy/README.md"
    example = strict_json(blocks[0])
    config = json.loads((REPO / "pipeline" / "config" / "lungpacer.json").read_text(encoding="utf-8"))
    company = example["company"]
    assert {k: v for k, v in example.items() if k not in ("company", "run_indirect")} == \
        {k: v for k, v in config.items() if k != "company"}
    assert {k: v for k, v in company.items() if k not in ("legal_name", "description")} == config["company"]
    assert company["legal_name"] and company["description"] and example["run_indirect"] is True
    cfg, out = tmp_path / "lungpacer.json", tmp_path / "lungpacer_input.csv"
    cfg.write_text(blocks[0], encoding="utf-8")
    r = subprocess.run([sys.executable, str(REPO / "tools" / "make_companies_file.py"), "lungpacer", "--config", str(cfg),
                        "--out", str(out), "--force"], capture_output=True, text=True, cwd=REPO,
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})       # no __pycache__ in pipeline/
    assert r.returncode == 0 and "WARNING" not in r.stderr, r.stderr
    (row,) = list(csv.DictReader(open(out, encoding="utf-8", newline="")))
    assert row == {"id": "C0001708893", "canonical_name": "Lungpacer Medical, Inc.", "aliases": "Lungpacer|AeroPace",
                   "description": company["description"]}
