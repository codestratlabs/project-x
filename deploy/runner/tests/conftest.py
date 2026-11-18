"""Shared test fixtures: moto S3 and Secrets Manager, fake secrets, and a fake pipeline in place of subprocess.Popen.

No real AWS call is made (moto), no pipeline script runs (FakePipeline plays each script's part: it checks that the
script's inputs were staged and writes outputs where the real script would), and every secret is a fake value.
"""
from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from px_runner import tasks

BUCKET, REGION = "test-company-reports", "us-east-2"
CO, RUN = "acme", "20261117T101500Z-1a2b3c4d"
SEC_ID, LLM_ID = "px/sec-user-agent", "px/openrouter-api-key"
FAKE_UA = "acme-research fake.contact@example.com"                     # fake values only
FAKE_KEY = "sk-or-v1-" + "0f1e2d3c4b5a6978" * 4
CONFIG = {"name": "Acme", "k401_plan": "001", "run_indirect": True,
          "company": {"legal_name": "Acme Widgets, Inc.", "aliases": ["Acme"], "name_regex": "acme", "cik": "0000000042",
                      "employee_company": "Acme Widgets"}}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """No real AWS settings, profile or secret from the developer's environment reaches a test."""
    for k in list(os.environ):
        if (k.startswith(("AWS_", "PX_")) and not k.startswith("PX_TEST_")) or k in ("SEC_USER_AGENT", "OPENROUTER_API_KEY"):
            monkeypatch.delenv(k, raising=False)
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                 "AWS_DEFAULT_REGION": REGION, "AWS_CONFIG_FILE": str(tmp_path / "no-aws-config"),
                 "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "no-aws-credentials")}.items():
        monkeypatch.setenv(k, v)


class Spy:
    """Secrets Manager client that records which secret ids were read (and offers nothing else)."""

    def __init__(self, client):
        self.client, self.ids = client, []

    def get_secret_value(self, SecretId):
        self.ids.append(SecretId)
        return self.client.get_secret_value(SecretId=SecretId)


@pytest.fixture
def aws():
    """(s3, secrets spy) on moto, with the bucket and both fake secrets."""
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        sm = boto3.client("secretsmanager", region_name=REGION)
        sm.create_secret(Name=SEC_ID, SecretString=FAKE_UA)
        sm.create_secret(Name=LLM_ID, SecretString=json.dumps({"OPENROUTER_API_KEY": FAKE_KEY}))
        yield s3, Spy(sm)


@pytest.fixture
def settings(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pipeline" / "config").mkdir(parents=True)
    return tasks.Settings(bucket=BUCKET, region=REGION, secret_sec=SEC_ID, secret_llm=LLM_ID, local_root=tmp_path / "px",
                          repo=repo, python="/opt/fake/bin/python", workers=3, nport_db="")


# ---------------------------------------------------------------------- S3 helpers
def put(s3, key: str, body) -> None:
    s3.put_object(Bucket=BUCKET, Key=key, Body=body if isinstance(body, bytes) else str(body).encode())


def get(s3, key: str) -> bytes:
    return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()


def keys(s3, prefix: str = "") -> set[str]:
    out = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        out.update(o["Key"] for o in page.get("Contents", []))
    return out


def record(s3, run_id: str = RUN) -> dict:
    return json.loads(get(s3, f"runs/{CO}/{run_id}.json"))


def seed(s3, config: dict | None = None, nport: bool = True, welfare: bool = False) -> None:
    """The company input and the shared reference data (fake contents)."""
    put(s3, f"input/{CO}/company.json", json.dumps(config or CONFIG, indent=1))
    put(s3, "reference/formd/formd.duckdb", b"fake duckdb")
    for name in ("form5500_rows.csv", "form5500sf_rows.csv", "schedule_h_rows.csv"):
        put(s3, f"reference/dol/raw/{name}", "company,ACK_ID\nAcme Widgets,1\n")
    if welfare:
        put(s3, "reference/dol/data/welfare_plans_by_year.csv", "company,plan_year\nAcme Widgets,2024\n")
    if nport:
        put(s3, f"reference/nport/{CO}/nport_holdings.csv", "report_date,position_type\n2025-06-30,equity\n")


# ---------------------------------------------------------------------- the fake pipeline
def w(path, text: str = "x\n") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def need(path) -> None:
    assert Path(path).exists(), f"input not staged: {path}"


def opt(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def make_xlsx(path: Path, sheets: dict[str, int]) -> None:
    """A minimal workbook: each sheet has a header row and n data rows. Odd sheets use an absolute part name."""
    m = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    r = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f"<workbook {m} {r}><sheets>" + "".join(
            f'<sheet name="{n}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(sheets, 1)) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i}" Type="{rel}" Target="{"/xl/" if i % 2 else ""}worksheets/sheet{i}.xml"/>'
                             for i in range(1, len(sheets) + 1)) + "</Relationships>")
        for i, n in enumerate(sheets.values(), 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", f"<worksheet {m}><sheetData>" + "".join(
                f'<row r="{k}"><c r="A{k}" t="inlineStr"><is><t>v</t></is></c></row>' for k in range(1, n + 2))
                + "</sheetData></worksheet>")


class FakeProc:
    def __init__(self, lines: list[str], code: int):
        self.stdout, self.returncode = iter(line + "\n" for line in lines), code

    def wait(self):
        return self.returncode

    def kill(self):
        pass


class FakePipeline:
    """Replaces subprocess.Popen in px_runner.tasks. Records every call; `fail[script] = fn(args, env) -> (code, lines)`
    makes a script fail."""

    def __init__(self):
        self.calls: list[SimpleNamespace] = []
        self.fail: dict = {}

    def __call__(self, cmd, cwd=None, env=None, **kw):
        self.calls.append(SimpleNamespace(cmd=list(cmd), cwd=cwd, env=dict(env or {})))
        script, args = cmd[1], list(cmd[2:])
        if script in self.fail:
            code, lines = self.fail[script](args, env)
            return FakeProc(lines, code)
        return FakeProc(SCRIPTS[script](args, env) or [], 0)

    def commands(self) -> list[list[str]]:
        return [c.cmd[1:] for c in self.calls]


def _companies(args, env):
    need(opt(args, "--config"))
    w(Path(env["PX_FINDER_ROOT"]) / "input" / f"{args[0]}_input.csv",
      'id,canonical_name,aliases,description\nC0000000042,"Acme Widgets, Inc.",Acme,\n')
    return ["wrote companies file"]


def _download(args, env):
    co, dumps = args[0], Path(opt(args, "--dumps"))
    old = dumps / co / "10-K" / "0000000042-25-000001_a.htm"
    if old.exists():                                    # a file SEC serves again with a different size
        old.write_text(old.read_text() + "<!-- changed -->")
    w(dumps / co / "10-Q" / "0000000042-26-000002_b.htm", "<html>Acme Widgets</html>")
    files = sorted(p.relative_to(dumps / co).as_posix() for p in (dumps / co).rglob("*.htm"))
    w(dumps / f"{co}_manifest.csv", "file,form,file_date,accession,filer,sec_url,index_url,status\n"
      + "".join(f"{f},10-K,2026-01-01,acc,Filer,https://x/{f},https://x/i,ok\n" for f in files)
      + "10-K/missing.htm,10-K,2026-01-02,acc2,Filer,https://x/m,https://x/i,HTTP 404\n")
    return ['{"downloaded_now": 1}']


def _finder(args, env):
    need(opt(args, "--companies"))
    assert any(Path(opt(args, "--input")).rglob("*.htm")), "no filings staged"
    out = Path(opt(args, "--out"))
    assert out.parent.is_dir(), "company_finder.py does not create its --out folder"
    w(out, "company,document\nAcme,b.htm\n")
    w(out.with_name(out.stem + "_mentions.csv"), "company_id,document,page\nC1,a.htm,1\nC1,b.htm,2\nC1,b.htm,3\n")
    w(out.with_name(out.stem + "_dropped.csv"), "company_id,document\nC1,c.htm\n")
    w(out.with_name(out.stem + "_summary.csv"), "company,mentions\nAcme,3\n")
    return ["3 mentions"]


def _nport(args, env):
    dump = Path(opt(args, "--dump"))
    if args[0] == "export-db":
        need(opt(args, "--db"))
        w(dump / "duckdb_rows.csv", "series_id,ISSUER_NAME\nS1,ACME WIDGETS\n")
    else:
        need(dump / "duckdb_rows.csv")
        w(opt(args, "--out"), "report_date,position_type,source\n2025-06-30,equity,team database\n")
    return ["1 rows"]


def _records(args, env):
    co, emp = args[0], Path(opt(args, "--employee-dir"))
    need(opt(args, "--formd-db"))
    need(emp / "form5500_rows.csv")
    data, lines = Path(env["PX_WORK"]) / "inputs" / co / "data", []
    if (Path(env["PX_DUMPS"]) / f"{co}_nport" / "nport_holdings.csv").exists():
        w(data / "nport_price_monthly.csv", "month,positions\n2025-06,1\n")
        lines.append("N-PORT price marks: {'nport_price_monthly.csv': 1}")
    else:
        lines.append("MISSING N-PORT price marks: FileNotFoundError: [Errno 2] No such file or directory: 'nport_holdings.csv'")
    w(data / "dol_form5500.csv", "FORM_PLAN_YEAR_BEGIN_DATE,SPONS_DFE_PN\n2023-01-01,001\n2024-01-01,001\n")
    w(data / "plan_participants.csv", "plan_year,plan_number\n2024,001\n")
    if (emp.parent / "data" / "welfare_plans_by_year.csv").exists():
        w(data / "dol_5500_sch_h.csv", "SCH_H_PLAN_YEAR_BEGIN_DATE\n2024-01-01\n")
        lines.append("DOL Form 5500: {'dol_form5500.csv': 2}")
    else:
        lines.append("MISSING DOL Form 5500: FileNotFoundError: [Errno 2] No such file or directory: 'welfare_plans_by_year.csv'")
    w(Path(env["PX_WORK"]) / "out" / co / "formd_offerings.csv", "accession_number,filing_date\nA1,2021-01-01\nA2,2022-01-01\n")
    lines.append("Form D offerings: {'formd_offerings.csv': 2}")
    missing = [x.split(":")[0].removeprefix("MISSING ") for x in lines if x.startswith("MISSING")]
    return lines + [f"not built: {missing or 'none'}"]


def _evidence(name):
    def fake(args, env):
        co = args[0]
        ev, dumps = Path(env["PX_WORK"]) / "out" / co / "evidence", Path(env["PX_DUMPS"])
        if name == "evidence_tables.py":
            need(dumps / f"{co}_manifest.csv")
            need(dumps / co / "10-Q" / "0000000042-26-000002_b.htm")
            need(Path(env["PX_FINDER_ROOT"]) / "out" / co / f"{co}_mentions.csv")
            need(Path(env["PX_WORK"]) / "inputs" / co / "data" / "plan_participants.csv")
            need(Path(env["PX_WORK"]) / "out" / co / "formd_offerings.csv")
        files = {"evidence_tables.py": "parsed_rows.csv", "evidence_build.py": "evidence_rows.csv",
                 "evidence_holdings.py": "holdings_from_evidence.csv", "validate_all.py": "validation.csv",
                 "evidence_facts.py": "facts_from_evidence.csv", "locate.py": "locator.csv"}
        w(ev / files[name], "evidence_id,value\nE1,1\nE2,2\nE3,3\nE4,4\n" if name == "evidence_build.py"
          else "id,value\nH1,1\nH2,2\n")
        return [f"{name} done"]
    return fake


def _prepare(args, env):
    co, finder, work = args[0], Path(env["PX_FINDER_ROOT"]), Path(env["PX_WORK"])
    need(finder / "out" / co / f"{co}_mentions.csv")
    need(finder / "input" / f"{co}_input.csv")
    tag = f"{co}__10-Q__0000000042-26-000002__0000000042-26-000002_b"
    w(work / "prepared" / "pdfs" / "0000000042-26-000002_b.htm", "<html>Acme Widgets</html>")
    w(work / "prepared" / "companies" / f"{tag}_mentions.csv", "company_id,page\nC1,1\n")
    w(work / "prepared" / "companies" / f"{tag}_mentions.meta.json", json.dumps({"document": "0000000042-26-000002_b.htm"}))
    return ["prepared 1 filing(s)"]


def _run_all(args, env):
    out = Path(args[1])
    metas = sorted((Path(env["PX_WORK"]) / "prepared" / "companies").glob("*.meta.json"))
    assert metas, "prepared filings not staged"
    done = {}
    for meta in metas:
        stem = Path(json.loads(meta.read_text())["document"]).stem
        if (out / f"{stem}_indirect_run.json").exists():
            done["skip"] = done.get("skip", 0) + 1
            continue
        w(out / f"{stem}_indirect_run.json", json.dumps({"meta": {"failed_calls": 0, "cost_usd": 0.1449},
                                                          "calls": [{"cost_usd": 0.1}, {"cost_usd": 0.0449}]}))
        w(out / f"{stem}_indirect_all_triggers.csv", "trigger,decided_by\nit,llm\n")
        w(out / f"{stem}_indirect_mentions.csv", "trigger,company\n")       # indirect_mentions.py writes all three
        done["ok"] = done.get("ok", 0) + 1
    w(out / "run.log", "== 10-Q rc=0\n")
    return [f"0 of {len(metas)} {{}}", f"done {done}"]


def _locate_mentions(args, env):
    need(Path(args[1]) / "run.log")
    need(Path(env["PX_WORK"]) / "prepared" / "pdfs" / "0000000042-26-000002_b.htm")
    w(Path(env["PX_WORK"]) / "out" / args[0] / "indirect_located.csv", "run,form,trigger\nindirect,10-Q,it\n")
    return ["1 rows"]


def _findings_excel(args, env):
    co, name, dest = args
    work = Path(env["PX_WORK"])
    need(work / "out" / co / "evidence" / "evidence_rows.csv")
    need(work / "out" / co / "formd_offerings.csv")
    need(work / "inputs" / co / "data" / "plan_participants.csv")
    need(Path(env["PX_DUMPS"]) / f"{co}_manifest.csv")
    located = (work / "out" / co / "indirect_located.csv").exists()
    make_xlsx(Path(dest) / f"{name.split(',')[0].replace(' ', '_')}_findings.xlsx",
              {"About": 12, "All evidence": 4, "Indirect mentions": 1 if located else 0, "Form D offerings": 2})
    return ["workbook written"]


SCRIPTS = {
    "tools/make_companies_file.py": _companies,
    "tools/download_filings.py": _download,
    "company-finder/company_finder.py": _finder,
    "pipeline/sources/nport.py": _nport,
    "pipeline/sources/records.py": _records,
    **{f"pipeline/evidence/{n}": _evidence(n) for n in ("evidence_tables.py", "evidence_build.py", "evidence_holdings.py",
                                                        "validate_all.py", "evidence_facts.py", "locate.py")},
    "pipeline/prepare_filings.py": _prepare,
    "pipeline/indirect/run_all.py": _run_all,
    "pipeline/indirect/locate_mentions.py": _locate_mentions,
    "pipeline/findings_excel.py": _findings_excel,
}


@pytest.fixture
def pipeline(monkeypatch):
    fake = FakePipeline()
    monkeypatch.setattr(tasks.subprocess, "Popen", fake)
    return fake


def run_task(task: str, settings, aws, run_id: str = RUN) -> int:
    s3, sm = aws
    return tasks.run(task, CO, run_id, settings, s3, sm)


def run_tasks(names, settings, aws) -> None:
    """Run the tasks in order; a failure shows its run-record entry."""
    for t in names:
        code = run_task(t, settings, aws)
        assert code == 0, f"{t} failed: {record(aws[0])['tasks'].get(t)}"
