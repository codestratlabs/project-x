"""The tasks against moto with a fake pipeline: commands, staging and uploads, secrets, skipping, failures, the CLI."""
import hashlib
import json
import os
import signal
import stat
import sys
from pathlib import Path

import pytest
from conftest import (CO, CONFIG, FAKE_KEY, FAKE_UA, LLM_ID, RUN, SCRIPTS, SEC_ID, FakeProc, get, keys, make_xlsx, put,
                      record, run_task, run_tasks, seed, w)

from px_runner import __main__ as cli
from px_runner import runrecord, s3sync, tasks

PIPELINE = list(runrecord.PIPELINE)
I = f"intermediate/{CO}/{RUN}"


def expected_commands(s) -> dict:
    """The commands of deploy/INTERFACES.md for co=acme (CONFIG: cik 0000000042, legal name Acme Widgets, Inc.)."""
    R, cfg, ev = s.local_root, str(s.repo / "pipeline" / "config" / "acme.json"), "pipeline/evidence/"
    return {
        "aliases": [["tools/make_companies_file.py", "acme", "--config", cfg, "--force"]],
        "download": [["tools/download_filings.py", "acme", "--config", cfg, "--dumps", f"{R}/data/dumps"]],
        "finder": [["company-finder/company_finder.py", "--companies", f"{R}/finder/input/acme_input.csv", "--input",
                    f"{R}/data/dumps/acme", "--out", f"{R}/finder/out/acme/acme.csv", "--format", "csv", "--llm-disambiguate"]],
        "records": [["pipeline/sources/records.py", "acme", "--cik", "0000000042", "--formd-db",
                     f"{R}/data/input/formd/formd.duckdb", "--employee-dir", f"{R}/data/dumps/employee_docs/raw"]],
        "evidence": [[ev + "evidence_tables.py", "acme", "--folders", "acme", "--workers", "3"],
                     [ev + "evidence_build.py", "acme"],
                     [ev + "evidence_holdings.py", "acme"],
                     [ev + "validate_all.py", "acme"],
                     [ev + "evidence_holdings.py", "acme", "--use-validation"],
                     [ev + "evidence_facts.py", "acme", "Acme Widgets, Inc."],
                     [ev + "locate.py", "acme"],
                     ["pipeline/prepare_filings.py", "acme"]],
        "indirect": [["pipeline/indirect/run_all.py", "acme", f"{R}/work/out/acme/indirect", "--parallel", "3"]],
        "publish": [["pipeline/indirect/locate_mentions.py", "acme", f"{R}/work/out/acme/indirect"],
                    ["pipeline/findings_excel.py", "acme", "Acme Widgets, Inc.", f"{R}/reports"]],
        "finalize": [],
    }


def run_each(names, settings, aws, pipeline):
    """Run tasks in order; returns {task: (commands, secret ids read)}."""
    out = {}
    for t in names:
        n_calls, n_ids = len(pipeline.calls), len(aws[1].ids)
        code = run_task(t, settings, aws)
        assert code == 0, f"{t} failed: {record(aws[0])['tasks'].get(t)}"
        out[t] = (pipeline.commands()[n_calls:], aws[1].ids[n_ids:])
    return out


# ---------------------------------------------------------------------- the whole run
def test_full_run_commands_keys_and_record(aws, settings, pipeline, monkeypatch):
    s3, _ = aws
    seed(s3)
    put(s3, f"reports/{CO}/latest/Acme_Old_findings.xlsx", "an earlier run's workbook")
    monkeypatch.setenv("SEC_USER_AGENT", "inherited-ua-must-not-pass")      # neither may reach a subprocess
    monkeypatch.setenv("OPENROUTER_API_KEY", "inherited-key-must-not-pass")
    done = run_each([*PIPELINE, "finalize"], settings, aws, pipeline)

    assert {t: cmds for t, (cmds, _) in done.items()} == expected_commands(settings)
    for c in pipeline.calls:
        assert c.cmd[0] == settings.python and c.cwd == settings.repo
        assert {k: c.env[k] for k in ("PX_DATA", "PX_DUMPS", "PX_FINDER_ROOT", "PX_WORK", "PYTHONUNBUFFERED")} == {
            "PX_DATA": f"{settings.local_root}/data", "PX_DUMPS": f"{settings.local_root}/data/dumps",
            "PX_FINDER_ROOT": f"{settings.local_root}/finder", "PX_WORK": f"{settings.local_root}/work", "PYTHONUNBUFFERED": "1"}
        assert not [k for k in c.env if k.startswith("AWS_")]

    assert keys(s3) == {
        f"input/{CO}/company.json", "reference/formd/formd.duckdb", f"reference/nport/{CO}/nport_holdings.csv",
        *(f"reference/dol/raw/{n}" for n in ("form5500_rows.csv", "form5500sf_rows.csv", "schedule_h_rows.csv")),
        f"raw/{CO}/sec/manifest.csv", f"raw/{CO}/sec/files/10-Q/0000000042-26-000002_b.htm",
        f"{I}/mentions/input/acme_input.csv",
        *(f"{I}/mentions/out/acme/{n}" for n in ("acme.csv", "acme_mentions.csv", "acme_dropped.csv", "acme_summary.csv")),
        f"{I}/records/formd_offerings.csv", f"{I}/records/nport/nport_holdings.csv",
        *(f"{I}/records/inputs/data/{n}" for n in ("nport_price_monthly.csv", "dol_form5500.csv", "plan_participants.csv")),
        *(f"{I}/evidence/{n}" for n in ("parsed_rows.csv", "evidence_rows.csv", "holdings_from_evidence.csv",
                                        "validation.csv", "facts_from_evidence.csv", "locator.csv")),
        f"{I}/prepared/pdfs/0000000042-26-000002_b.htm",
        *(f"{I}/prepared/companies/acme__10-Q__0000000042-26-000002__0000000042-26-000002_b_mentions{x}"
          for x in (".csv", ".meta.json")),
        *(f"{I}/indirect/{n}" for n in ("0000000042-26-000002_b_indirect_run.json", "run.log",
                                        "0000000042-26-000002_b_indirect_all_triggers.csv",
                                        "0000000042-26-000002_b_indirect_mentions.csv",
                                        "0000000042-26-000002_b_indirect_input.json", "model_cost.json")),
        f"{I}/publish/indirect_located.csv",
        f"reports/{CO}/{RUN}/Acme_Widgets_findings.xlsx", f"reports/{CO}/latest/Acme_Widgets_findings.xlsx",
        f"runs/{CO}/{RUN}.json"}                     # the earlier latest/ workbook is replaced, nothing else deleted

    rec = record(s3)
    assert rec["status"] == "succeeded" and rec["ended_at"] and rec["started_at"]
    assert {t: rec["tasks"][t]["status"] for t in PIPELINE} == dict.fromkeys(PIPELINE, "succeeded")
    out = {t: rec["tasks"][t]["outputs"] for t in PIPELINE}
    assert out["download"] == {"manifest_rows": 2, "failed_downloads": 1, "new_or_changed_files": 1}
    assert out["finder"] == {"mentions": 3, "dropped": 1, "files_uploaded": 5}
    assert out["records"] == {"nport": "reference", "not_built": ["DOL Form 5500"], "files_uploaded": 5, "rows": {
        "formd_offerings.csv": 2, "inputs/data/dol_form5500.csv": 2, "inputs/data/nport_price_monthly.csv": 1,
        "inputs/data/plan_participants.csv": 1, "nport/nport_holdings.csv": 1}}
    assert out["evidence"] == {"evidence_rows": 4, "holdings": 2, "prepared_filings": 1, "files_uploaded": 9}
    assert out["indirect"] == {"filings": 1, "failed_filings": 0, "failed_calls": 0, "redone_filings": 0,
                               "model_cost_usd": 0.1449, "files_uploaded": 6}
    prepared = f"{I}/prepared/"                          # the input file: the prepared files the answers were made from
    assert json.loads(get(s3, f"{I}/indirect/0000000042-26-000002_b_indirect_input.json")) == {
        k.removeprefix(prepared): hashlib.sha256(get(s3, k)).hexdigest() for k in keys(s3, prepared)}
    (ledger,) = json.loads(get(s3, f"{I}/indirect/model_cost.json")).values()
    assert ledger == {"filing": "0000000042-26-000002_b", "cost_usd": 0.1449}
    assert out["publish"] == {"workbook": f"reports/{CO}/{RUN}/Acme_Widgets_findings.xlsx", "indirect": True,
                              "files_uploaded": 3, "sheets": {"About": 12, "All evidence": 4, "Indirect mentions": 1,
                                                              "Form D offerings": 2}}
    assert rec["model_cost_usd"] == {"finder": None, "indirect": 0.1449}
    assert rec["workbook"] == f"reports/{CO}/{RUN}/Acme_Widgets_findings.xlsx"


def test_each_task_reads_only_its_own_secret(aws, settings, pipeline, monkeypatch):
    seed(aws[0])
    monkeypatch.setenv("SEC_USER_AGENT", "inherited-ua-must-not-pass")      # in the runner's own environment:
    monkeypatch.setenv("OPENROUTER_API_KEY", "inherited-key-must-not-pass")  # never handed to a script
    done = run_each([*PIPELINE, "finalize"], settings, aws, pipeline)
    assert {t: ids for t, (_, ids) in done.items()} == {
        "aliases": [], "download": [SEC_ID], "finder": [LLM_ID], "records": [], "evidence": [], "indirect": [LLM_ID],
        "publish": [], "finalize": []}
    for c in pipeline.calls:
        script = c.cmd[1]
        assert c.env.get("SEC_USER_AGENT") == (FAKE_UA if script == "tools/download_filings.py" else None)
        assert c.env.get("OPENROUTER_API_KEY") == (
            FAKE_KEY if script in ("company-finder/company_finder.py", "pipeline/indirect/run_all.py") else None)


def test_run_indirect_false_skips_task_6_without_reading_the_key(aws, settings, pipeline):
    s3, sm = aws
    seed(s3, config={**CONFIG, "run_indirect": False})
    run_tasks(PIPELINE[:5], settings, aws)
    n_calls, n_ids = len(pipeline.calls), len(sm.ids)
    assert run_task("indirect", settings, aws) == 0
    assert len(pipeline.calls) == n_calls and sm.ids[n_ids:] == []
    entry = record(s3)["tasks"]["indirect"]
    assert entry["status"] == "skipped" and entry["outputs"] == {"reason": "run_indirect is false"}
    assert not keys(s3, f"{I}/indirect/")
    run_tasks(["publish"], settings, aws)
    assert [c[0] for c in pipeline.commands()[n_calls:]] == ["pipeline/findings_excel.py"]   # no locate_mentions.py
    assert f"{I}/publish/indirect_located.csv" not in keys(s3)
    pub = record(s3)["tasks"]["publish"]["outputs"]
    assert pub["indirect"] is False and pub["sheets"]["Indirect mentions"] == 0
    assert run_task("finalize", settings, aws) == 0 and record(s3)["status"] == "succeeded"
    assert record(s3)["model_cost_usd"] == {"finder": None, "indirect": None}


# ---------------------------------------------------------------------- failures
def test_failed_script_records_the_last_40_lines_without_secrets(aws, settings, pipeline, capsys):
    s3, _ = aws
    seed(s3)
    run_tasks(["aliases"], settings, aws)

    def noisy_failure(args, env):
        lines = [f"line {i}" for i in range(1, 61)]
        lines[55] = f"User-Agent: {env['SEC_USER_AGENT']}"            # a script that prints its secret
        lines[57] = f"retrying with Authorization: Bearer {FAKE_KEY}"
        return 3, lines
    pipeline.fail["tools/download_filings.py"] = noisy_failure
    assert run_task("download", settings, aws) == 1
    entry = record(s3)["tasks"]["download"]
    assert entry["status"] == "failed" and entry["outputs"] == {} and entry["ended_at"]
    err = entry["error"].splitlines()
    assert err[0] == "tools/download_filings.py exited with code 3" and len(err) == 41
    assert err[1] == "line 21" and err[-1] == "line 60"                 # the last 40 lines only
    printed = capsys.readouterr().out
    for text in (entry["error"], printed, json.dumps(record(s3))):
        assert FAKE_UA not in text and FAKE_KEY not in text and "fake.contact@example.com" not in text
    assert "User-Agent: [redacted]" in entry["error"] and "line 1\n" in printed      # all output is streamed
    assert f"raw/{CO}/sec/manifest.csv" not in keys(s3)                 # nothing uploaded after a failure
    # finalize records the verdict and exits 0 (the DAG fails a failed run from its task states): a non-zero finalize
    # means only that finalize itself did not work
    assert run_task("finalize", settings, aws) == 0 and record(s3)["status"] == "failed"


@pytest.mark.parametrize("config,message", [
    (None, f"missing s3://test-company-reports/input/{CO}/company.json"),
    ({**CONFIG, "run_indirect": "yes"}, "run_indirect must be true or false"),
    ({"name": "Acme", "company": "not an object"}, "expected a JSON object"),
])
def test_bad_or_missing_input_fails_before_any_script(aws, settings, pipeline, config, message):
    s3, _ = aws
    if config is not None:
        put(s3, f"input/{CO}/company.json", json.dumps(config))
    assert run_task("aliases", settings, aws) == 1
    entry = record(s3)["tasks"]["aliases"]
    assert entry["status"] == "failed" and message in entry["error"] and pipeline.calls == []


def test_records_fails_without_the_form_d_file(aws, settings, pipeline):
    s3, _ = aws
    seed(s3)
    s3.delete_object(Bucket="test-company-reports", Key="reference/formd/formd.duckdb")
    assert run_task("records", settings, aws) == 1
    assert "missing s3://test-company-reports/reference/formd/formd.duckdb" in record(s3)["tasks"]["records"]["error"]
    assert pipeline.calls == []


def test_records_without_any_nport_source_is_not_a_failure(aws, settings, pipeline):
    s3, _ = aws
    seed(s3, nport=False, welfare=True)
    run_tasks(["records"], settings, aws)
    out = record(s3)["tasks"]["records"]["outputs"]
    assert out["nport"] == "none" and out["not_built"] == ["N-PORT price marks"]
    assert not keys(s3, f"{I}/records/nport/") and f"{I}/records/inputs/data/dol_5500_sch_h.csv" in keys(s3)


def test_records_builds_nport_from_the_team_database(aws, settings, pipeline, tmp_path):
    s3, _ = aws
    seed(s3)                                                             # the reference file must then be ignored
    db = tmp_path / "nport_team.duckdb"
    db.write_text("fake")
    s = tasks.Settings(**{**settings.__dict__, "nport_db": str(db)})
    run_tasks(["records"], s, aws)
    R, cfg, nport = s.local_root, str(s.repo / "pipeline/config/acme.json"), f"{s.local_root}/data/dumps/acme_nport"
    assert pipeline.commands() == [
        ["pipeline/sources/nport.py", "export-db", "--dump", nport, "--db", str(db), "--config", cfg],
        ["pipeline/sources/nport.py", "build", "--dump", nport, "--out", f"{nport}/nport_holdings.csv", "--config", cfg],
        ["pipeline/sources/records.py", "acme", "--cik", "0000000042", "--formd-db", f"{R}/data/input/formd/formd.duckdb",
         "--employee-dir", f"{R}/data/dumps/employee_docs/raw"]]
    assert record(s3)["tasks"]["records"]["outputs"]["nport"] == "database"
    assert {f"{I}/records/nport/duckdb_rows.csv", f"{I}/records/nport/nport_holdings.csv"} <= keys(s3)
    assert get(s3, f"{I}/records/nport/nport_holdings.csv").endswith(b"team database\n")   # built, not the reference copy


def test_download_uploads_new_and_changed_files_and_never_deletes(aws, settings, pipeline):
    s3, _ = aws
    seed(s3)
    put(s3, f"raw/{CO}/sec/files/10-K/0000000042-25-000001_a.htm", "<html>old a</html>")
    put(s3, f"raw/{CO}/sec/files/8-K/0000000042-25-000003_c.htm", "<html>c</html>")
    put(s3, f"raw/{CO}/sec/files/8-K/only-in-s3.htm", "<html>kept</html>")
    run_tasks(["download"], settings, aws)
    assert record(s3)["tasks"]["download"]["outputs"] == {"manifest_rows": 5, "failed_downloads": 1,
                                                           "new_or_changed_files": 2}
    raw = {k.removeprefix(f"raw/{CO}/sec/files/") for k in keys(s3, f"raw/{CO}/sec/files/")}
    assert raw == {"10-K/0000000042-25-000001_a.htm", "10-Q/0000000042-26-000002_b.htm",
                   "8-K/0000000042-25-000003_c.htm", "8-K/only-in-s3.htm"}
    assert get(s3, f"raw/{CO}/sec/files/10-K/0000000042-25-000001_a.htm").endswith(b"<!-- changed -->")
    assert b"0000000042-25-000003_c.htm" in get(s3, f"raw/{CO}/sec/manifest.csv")


def test_indirect_failures_keep_cost_and_a_retry_skips_finished_filings(aws, settings, pipeline):
    s3, _ = aws
    seed(s3)
    run_tasks(PIPELINE[:5], settings, aws)

    def crash_after_one_filing(args, env):
        SCRIPTS["pipeline/indirect/run_all.py"](args, env)               # one filing finished, then the script dies
        return 1, ["Traceback (most recent call last):", "MemoryError"]
    pipeline.fail["pipeline/indirect/run_all.py"] = crash_after_one_filing
    assert run_task("indirect", settings, aws) == 1
    entry = record(s3)["tasks"]["indirect"]
    assert entry["status"] == "failed" and entry["error"].startswith("pipeline/indirect/run_all.py exited with code 1")
    assert entry["outputs"]["model_cost_usd"] == 0.1449 and record(s3)["model_cost_usd"]["indirect"] == 0.1449
    assert f"{I}/indirect/0000000042-26-000002_b_indirect_run.json" in keys(s3)

    del pipeline.fail["pipeline/indirect/run_all.py"]                   # Airflow retry: the finished filing is skipped
    assert run_task("indirect", settings, aws) == 0
    assert record(s3)["tasks"]["indirect"]["outputs"] == {"filings": 1, "failed_filings": 0, "failed_calls": 0,
                                                          "redone_filings": 0, "model_cost_usd": 0.1449,
                                                          "files_uploaded": 6}
    assert pipeline.commands()[-1][0] == "pipeline/indirect/run_all.py"

    pipeline.fail["pipeline/indirect/run_all.py"] = lambda args, env: (0, ["done {'ok': 3, 'fail': 2}"])
    assert run_task("indirect", settings, aws) == 1
    entry = record(s3)["tasks"]["indirect"]
    assert "2 filing(s) failed" in entry["error"] and entry["outputs"]["failed_filings"] == 2


STEM = "0000000042-26-000002_b"                  # the filing the fake prepare_filings.py prepares


def test_indirect_failed_model_calls_fail_the_task_and_a_retry_runs_them_again(aws, settings, pipeline):
    """indirect_mentions.py exits 0 when its model calls fail (credit limit, 401, 5xx): the task must still fail. Each
    retry runs the filing again, and the cost of every attempt's answers stays in the record."""
    s3, _ = aws
    seed(s3)
    run_tasks(PIPELINE[:5], settings, aws)

    def calls_failed(cost):
        def fake(args, env):
            lines = SCRIPTS["pipeline/indirect/run_all.py"](args, env)
            (Path(args[1]) / f"{STEM}_indirect_run.json").write_text(json.dumps(
                {"meta": {"failed_calls": 2, "output_complete": False},
                 "calls": [{"cost_usd": cost, "error": ""}, {"cost_usd": 0, "error": "HTTP 402"}]}))
            return 0, lines                                             # run_all.py counts the filing ok
        return fake
    for attempt, (cost, spent) in enumerate(((0.02, 0.02), (0.03, 0.05))):
        pipeline.fail["pipeline/indirect/run_all.py"] = calls_failed(cost)
        assert run_task("indirect", settings, aws) == 1
        entry = record(s3)["tasks"]["indirect"]
        assert entry["status"] == "failed" and "1 filing(s) have model calls that failed" in entry["error"]
        assert entry["outputs"]["failed_calls"] == 2 and entry["outputs"]["failed_filings"] == 0
        assert entry["outputs"]["redone_filings"] == attempt and entry["outputs"]["model_cost_usd"] == spent
        assert f"{I}/indirect/{STEM}_indirect_run.json" in keys(s3)

    del pipeline.fail["pipeline/indirect/run_all.py"]                   # the retry runs that filing again
    assert run_task("indirect", settings, aws) == 0
    assert record(s3)["tasks"]["indirect"]["outputs"] == {
        "filings": 1, "failed_filings": 0, "failed_calls": 0, "redone_filings": 1, "files_uploaded": 6,
        "model_cost_usd": 0.1949}                                       # 0.1449 now, 0.02 and 0.03 for replaced answers
    assert record(s3)["model_cost_usd"]["indirect"] == 0.1949
    assert json.loads(get(s3, f"{I}/indirect/{STEM}_indirect_run.json"))["meta"]["failed_calls"] == 0


def test_indirect_stopped_uploads_what_finished_and_a_retry_skips_it(aws, settings, pipeline, monkeypatch):
    s3, _ = aws
    seed(s3)
    run_tasks(PIPELINE[:5], settings, aws)

    class Stopped(FakeProc):                                            # SIGTERM after one filing finished
        def __init__(self, lines):
            super().__init__([], 0)
            self.stdout = self.lines(lines)

        def lines(self, lines):
            yield from (line + "\n" for line in lines)
            raise tasks.Terminated("stopped by signal 15 (task timeout or container stop)")

    def popen(cmd, cwd=None, env=None, **kw):
        if cmd[1] == "pipeline/indirect/run_all.py":
            return Stopped(SCRIPTS[cmd[1]](list(cmd[2:]), env))
        return pipeline(cmd, cwd=cwd, env=env, **kw)
    monkeypatch.setattr(tasks.subprocess, "Popen", popen)
    assert run_task("indirect", settings, aws) == 1
    entry = record(s3)["tasks"]["indirect"]
    assert entry["error"].startswith("Terminated: stopped by signal 15") and entry["outputs"]["model_cost_usd"] == 0.1449
    assert {f"{I}/indirect/{STEM}{suffix}" for suffix in tasks.INDIRECT_FILES} <= keys(s3)

    monkeypatch.setattr(tasks.subprocess, "Popen", pipeline)
    assert run_task("indirect", settings, aws) == 0
    out = record(s3)["tasks"]["indirect"]["outputs"]
    assert (out["redone_filings"], out["model_cost_usd"]) == (0, 0.1449)      # skipped: not paid again


def test_indirect_runs_a_filing_again_when_its_results_are_incomplete(aws, settings, pipeline):
    """A run file without its two CSV files (an upload cut short) must not make run_all.py skip the filing."""
    s3, _ = aws
    seed(s3)
    run_tasks(PIPELINE[:5], settings, aws)
    put(s3, f"{I}/indirect/{STEM}_indirect_run.json", json.dumps({"meta": {"failed_calls": 0}, "calls": []}))
    assert run_task("indirect", settings, aws) == 0
    out = record(s3)["tasks"]["indirect"]["outputs"]
    assert (out["redone_filings"], out["filings"], out["model_cost_usd"]) == (1, 1, 0.1449)
    assert {f"{I}/indirect/{STEM}{suffix}" for suffix in tasks.INDIRECT_FILES} <= keys(s3)


def run_all_from_mentions(args, env):
    """run_all.py whose answers say which mentions file they were made from, as the real answers depend on it (the
    mentions decide the pronouns sent to the model); like run_all.py, it skips a filing that has a run file. Its run
    files are the same each time (the real ones hold their start time): each is still a model run paid for."""
    out, done = Path(args[1]), {}
    for meta in sorted((Path(env["PX_WORK"]) / "prepared" / "companies").glob("*.meta.json")):
        stem = Path(json.loads(meta.read_text())["document"]).stem
        if (out / f"{stem}{tasks.RUN_JSON}").exists():
            done["skip"] = done.get("skip", 0) + 1
            continue
        made_from = meta.with_name(meta.name.replace(".meta.json", ".csv")).read_text()
        w(out / f"{stem}_indirect_all_triggers.csv", "trigger,made_from\nit," + json.dumps(made_from) + "\n")
        w(out / f"{stem}_indirect_mentions.csv", "trigger,company\n")
        w(out / f"{stem}{tasks.RUN_JSON}", json.dumps({"meta": {"failed_calls": 0}, "calls": [{"cost_usd": 0.1}]}))
        done["ok"] = done.get("ok", 0) + 1
    w(out / "run.log", "== 10-Q rc=0\n")
    return 0, [f"done {done}"]


def test_indirect_runs_a_filing_again_when_its_prepared_mentions_changed(aws, settings, pipeline):
    """finder cleared with its downstream tasks after the input changed: a filing that is still prepared can have other
    mentions now, so the answers made from the earlier ones must not be kept (nor reach the Indirect mentions sheet)."""
    s3, _ = aws
    seed(s3)
    pipeline.fail["pipeline/indirect/run_all.py"] = run_all_from_mentions
    run_tasks(PIPELINE, settings, aws)
    triggers = f"{I}/indirect/{STEM}_indirect_all_triggers.csv"
    assert b"C1,1" in get(s3, triggers)

    def prepare_other_mentions(args, env):                              # the same filing, now with other mentions
        lines = SCRIPTS["pipeline/prepare_filings.py"](args, env)
        tag = f"acme__10-Q__0000000042-26-000002__{STEM}"
        w(Path(env["PX_WORK"]) / "prepared" / "companies" / f"{tag}_mentions.csv", "company_id,page\nC1,7\n")
        return 0, lines
    pipeline.fail["pipeline/prepare_filings.py"] = prepare_other_mentions
    run_tasks(["finder", "records", "evidence", "indirect", "publish"], settings, aws)
    out = record(s3)["tasks"]["indirect"]["outputs"]
    assert (out["filings"], out["redone_filings"], out["model_cost_usd"]) == (1, 1, 0.2)   # both answers were paid for
    assert b"C1,7" in get(s3, triggers) and b"C1,1" not in get(s3, triggers)

    run_tasks(["evidence", "indirect"], settings, aws)                  # the same mentions again: the answers are kept
    out = record(s3)["tasks"]["indirect"]["outputs"]
    assert (out["redone_filings"], out["model_cost_usd"]) == (0, 0.2) and b"C1,7" in get(s3, triggers)


class Killed(BaseException):
    """Stands in for SIGKILL: the runner stops where it is and records nothing (it catches only Exception)."""


def test_indirect_upload_cut_short_never_keeps_answers_of_two_attempts(aws, settings, pipeline, monkeypatch):
    """A stop starts the upload of what finished, and the stop timeout's SIGKILL can cut it short. The CSV files go up
    before the run file, so the earlier attempt's run file (with failed calls) stays beside them in S3, and the retry
    runs the filing again instead of publishing the earlier attempt's triggers."""
    s3, _ = aws
    seed(s3)
    run_tasks(PIPELINE[:5], settings, aws)

    def calls_failed(args, env):                                        # attempt 1: every model call failed, exit 0
        out = Path(args[1])
        w(out / f"{STEM}_indirect_all_triggers.csv", "trigger,decided_by,status\nit,llm,no_answer\n")
        w(out / f"{STEM}_indirect_mentions.csv", "trigger,company\n")
        w(out / f"{STEM}{tasks.RUN_JSON}", json.dumps({"meta": {"failed_calls": 2}, "calls": [{"cost_usd": 0.02}]}))
        return 0, ["done {'ok': 1}"]
    pipeline.fail["pipeline/indirect/run_all.py"] = calls_failed
    assert run_task("indirect", settings, aws) == 1
    del pipeline.fail["pipeline/indirect/run_all.py"]

    real_upload = s3sync.Bucket.upload

    def killed_during_the_csv_files(self, path, key):                   # attempt 2: answers redone, then SIGKILL
        if key.endswith(".csv"):
            raise Killed()
        return real_upload(self, path, key)
    monkeypatch.setattr(s3sync.Bucket, "upload", killed_during_the_csv_files)
    with pytest.raises(Killed):
        run_task("indirect", settings, aws)
    run_file = f"{I}/indirect/{STEM}{tasks.RUN_JSON}"
    assert json.loads(get(s3, run_file))["meta"]["failed_calls"] == 2  # the new run file goes up after the CSV files

    order = []

    def recorded(self, path, key):
        order.append(key.rsplit("/", 1)[1])
        return real_upload(self, path, key)
    monkeypatch.setattr(s3sync.Bucket, "upload", recorded)
    assert run_task("indirect", settings, aws) == 0                     # attempt 3
    rank = [2 if n.endswith(tasks.INPUT_JSON) else 1 if n.endswith(tasks.RUN_JSON) else 0 for n in order]
    assert rank == sorted(rank) and rank.count(1) == rank.count(2) == 1  # CSV files, then the run file, then the input
    out = record(s3)["tasks"]["indirect"]["outputs"]
    assert (out["redone_filings"], out["failed_calls"]) == (1, 0)
    assert b"no_answer" not in get(s3, f"{I}/indirect/{STEM}_indirect_all_triggers.csv")
    # the killed attempt's answers were paid for too: its ledger went up with the CSV files' pass
    assert out["model_cost_usd"] == round(0.02 + 0.1449 + 0.1449, 4)


class DeletesRefused:
    """The S3 client, except that DeleteObjects answers as S3 does for keys the role may not delete: HTTP 200, every
    key under Errors, nothing deleted (boto3 raises nothing; moto refuses the whole call instead)."""

    def __init__(self, s3):
        self.s3 = s3

    def __getattr__(self, name):
        return getattr(self.s3, name)

    def delete_objects(self, Bucket, Delete):
        return {"Errors": [{"Key": o["Key"], "Code": "AccessDenied", "Message": "Access Denied"} for o in Delete["Objects"]]}


def test_a_task_fails_when_s3_refuses_to_delete_an_earlier_attempts_files(aws, settings, pipeline):
    """A task role without s3:DeleteObject on intermediate/ (one made from an older policy): the earlier attempt's
    filing would stay prepared, so the task fails with the reason instead of succeeding."""
    s3, sm = aws
    seed(s3)
    run_tasks(PIPELINE[:5], settings, aws)
    stale = f"{I}/prepared/pdfs/0000000042-25-000009_z.htm"
    put(s3, stale, "<html>prepared by an earlier attempt</html>")
    assert run_task("evidence", settings, (DeletesRefused(s3), sm)) == 1
    entry = record(s3)["tasks"]["evidence"]
    assert entry["status"] == "failed" and entry["error"].startswith("DeleteFailed: S3 did not delete 1 of 1 object(s)")
    assert f"{stale} (AccessDenied" in entry["error"] and "s3:DeleteObject" in entry["error"] and stale in keys(s3)


def test_a_rerun_task_leaves_no_files_of_an_earlier_attempt(aws, settings, pipeline):
    """Cleared with its downstream tasks (say the input changed), each task's folders hold only what its latest attempt
    wrote, so a filing that is no longer prepared gets no model call and reaches no sheet."""
    s3, _ = aws
    seed(s3)
    run_tasks(PIPELINE, settings, aws)
    old = "0000000042-25-000009_z"                                      # prepared by an earlier attempt only
    tag = f"acme__10-K__0000000042-25-000009__{old}"
    stale = {f"{I}/mentions/out/acme/acme_extra.csv": "x\n", f"{I}/records/nport/duckdb_rows.csv": "series_id\nS1\n",
             f"{I}/records/inputs/data/old_table.csv": "x\n1\n", f"{I}/evidence/table_cache/{old}.json": "{}",
             f"{I}/prepared/companies/{tag}_mentions.csv": "company_id,page\nC1,1\n",
             f"{I}/prepared/companies/{tag}_mentions.meta.json": json.dumps({"document": f"{old}.htm"}),
             f"{I}/prepared/pdfs/{old}.htm": "<html>Acme</html>",
             **{f"{I}/indirect/{old}{suffix}": json.dumps({"meta": {}, "calls": [{"cost_usd": 1}]})
                for suffix in tasks.INDIRECT_FILES}}
    for key, body in stale.items():
        put(s3, key, body)
    before = keys(s3) - set(stale)
    run_tasks(["finder", "records", "evidence", "indirect", "publish"], settings, aws)
    assert keys(s3) == before                                           # the stale keys are gone, nothing else
    out = record(s3)["tasks"]["indirect"]["outputs"]
    # the prepared filing was finished earlier (skipped); the dropped filing's answers count in the cost (1 USD)
    assert (out["filings"], out["redone_filings"], out["model_cost_usd"]) == (1, 0, 1.1449)


def test_publish_leaves_out_indirect_files_when_task_6_did_not_run(aws, settings, pipeline):
    s3, _ = aws
    seed(s3)
    run_tasks(PIPELINE, settings, aws)                                  # task 6 ran: indirect/ and publish/ have files
    put(s3, f"input/{CO}/company.json", json.dumps({**CONFIG, "run_indirect": False}))
    n = len(pipeline.calls)
    run_tasks(["indirect", "publish"], settings, aws)                   # cleared after run_indirect was switched off
    assert record(s3)["tasks"]["indirect"]["status"] == "skipped"
    assert [c[0] for c in pipeline.commands()[n:]] == ["pipeline/findings_excel.py"]     # no locate_mentions.py
    pub = record(s3)["tasks"]["publish"]["outputs"]
    assert pub["indirect"] is False and pub["sheets"]["Indirect mentions"] == 0
    assert f"{I}/publish/indirect_located.csv" not in keys(s3)          # the earlier attempt's located rows are gone


def test_records_fails_when_records_py_cannot_build_form_d(aws, settings, pipeline):
    """records.py exits 0 with MISSING Form D offerings (a Form D file duckdb cannot open, a missing column)."""
    s3, _ = aws
    seed(s3)

    def unreadable_formd(args, env):
        lines = SCRIPTS["pipeline/sources/records.py"](args, env)
        (Path(env["PX_WORK"]) / "out" / args[0] / "formd_offerings.csv").unlink()
        bad = "MISSING Form D offerings: IOException: IO Error: Trying to read a database file with version number 99"
        return 0, [x for x in lines[:-1] if not x.startswith("Form D")] + [bad, "not built: [...]"]
    pipeline.fail["pipeline/sources/records.py"] = unreadable_formd
    assert run_task("records", settings, aws) == 1
    entry = record(s3)["tasks"]["records"]
    assert entry["status"] == "failed" and entry["error"].startswith("records.py could not build the Form D offerings")
    assert "version number 99" in entry["error"] and entry["outputs"]["not_built"] == ["DOL Form 5500", "Form D offerings"]
    assert not keys(s3, f"{I}/records/")                                # nothing uploaded


def test_records_fails_without_form_d_output_even_when_the_error_left_the_tail(aws, settings, pipeline):
    """A long error message after the MISSING line pushes it out of the 40 lines kept: the missing file still tells."""
    s3, _ = aws
    seed(s3)

    def long_formd_error(args, env):
        lines = SCRIPTS["pipeline/sources/records.py"](args, env)
        (Path(env["PX_WORK"]) / "out" / args[0] / "formd_offerings.csv").unlink()
        error = ["MISSING Form D offerings: BinderException: Binder Error: column issuer_name not found"]
        error += [f"  candidate {i}" for i in range(45)]
        return 0, [x for x in lines[:-1] if not x.startswith("Form D")] + error
    pipeline.fail["pipeline/sources/records.py"] = long_formd_error
    assert run_task("records", settings, aws) == 1
    entry = record(s3)["tasks"]["records"]
    assert entry["error"].startswith("records.py could not build the Form D offerings:\n"
                                      "it wrote no formd_offerings.csv")
    assert "Form D offerings" in entry["outputs"]["not_built"] and not keys(s3, f"{I}/records/")


def test_terminate_during_a_script_kills_it_and_records_the_failure(aws, settings, monkeypatch):
    s3, _ = aws
    seed(s3)
    killed = []

    class Dying(FakeProc):
        def __init__(self):
            super().__init__([], 0)
            self.stdout = self.lines()

        def lines(self):
            yield "working\n"
            raise tasks.Terminated("stopped by signal 15 (task timeout or container stop)")

        def kill(self):
            killed.append(True)
    monkeypatch.setattr(tasks.subprocess, "Popen", lambda *a, **k: Dying())
    assert run_task("aliases", settings, aws) == 1
    entry = record(s3)["tasks"]["aliases"]
    assert killed == [True] and entry["status"] == "failed" and entry["error"].startswith("Terminated: stopped by signal 15")


# ---------------------------------------------------------------------- local disk, settings, CLI, workbook
def test_local_root_is_wiped_and_the_config_written_as_is(aws, settings, pipeline):
    s3, _ = aws
    seed(s3)
    stale = settings.local_root / "work" / "out" / "acme" / "stale.csv"
    stale.parent.mkdir(parents=True)
    stale.write_text("from another task")
    run_tasks(["aliases"], settings, aws)
    assert not stale.exists() and stat.S_IMODE(settings.local_root.stat().st_mode) == 0o700     # private to the runner
    assert (settings.repo / "pipeline" / "config" / "acme.json").read_bytes() == get(s3, f"input/{CO}/company.json")


def test_a_local_root_that_is_a_symlink_is_neither_followed_nor_wiped(aws, settings, pipeline, tmp_path):
    """On a shared host another user can create /tmp/px as a link to any folder the runner's user may delete."""
    s3, _ = aws
    seed(s3)
    victim = tmp_path / "someone-elses-folder"
    (victim / "keep").mkdir(parents=True)
    (victim / "keep" / "data.txt").write_text("must survive")
    link = tmp_path / "px-link"
    link.symlink_to(victim, target_is_directory=True)
    s = tasks.Settings(**{**settings.__dict__, "local_root": link})
    assert run_task("aliases", s, aws) == 1
    assert "is a symbolic link; refusing" in record(s3)["tasks"]["aliases"]["error"] and pipeline.calls == []
    assert (victim / "keep" / "data.txt").read_text() == "must survive" and link.is_symlink()


def test_a_local_root_of_another_user_is_not_wiped(aws, settings, pipeline, monkeypatch):
    s3, _ = aws
    seed(s3)
    (settings.local_root / "theirs").mkdir(parents=True)
    uid = os.getuid()
    monkeypatch.setattr(tasks.os, "getuid", lambda: uid + 1)            # the folder now belongs to "another" user
    assert run_task("aliases", settings, aws) == 1
    assert "belongs to another user" in record(s3)["tasks"]["aliases"]["error"]
    assert (settings.local_root / "theirs").is_dir() and pipeline.calls == []


@pytest.mark.parametrize("root", ["/", "HOME", "REPO", "REPO_PARENT"])
def test_refuses_to_wipe_a_local_root_that_is_not_scratch(aws, settings, pipeline, root, monkeypatch):
    s3, _ = aws
    seed(s3)
    monkeypatch.setattr(tasks.shutil, "rmtree", lambda p: pytest.fail(f"would have wiped {p}"))   # never really delete
    path = {"/": Path("/"), "HOME": Path.home(), "REPO": settings.repo, "REPO_PARENT": settings.repo.parent}[root]
    s = tasks.Settings(**{**settings.__dict__, "local_root": path})
    assert run_task("aliases", s, aws) == 1
    assert "refusing to wipe" in record(s3)["tasks"]["aliases"]["error"] and settings.repo.exists()


def test_settings_defaults_and_overrides():
    s = tasks.Settings.from_env({})
    assert (s.bucket, s.region, s.secret_sec, s.secret_llm, s.local_root, s.python, s.workers, s.nport_db) == (
        "company-reports-data", "us-east-2", "px/sec-user-agent", "px/openrouter-api-key", Path("/tmp/px"),
        sys.executable, 4, "")
    assert s.repo == Path(__file__).resolve().parents[3] == tasks.find_repo()     # this checkout
    s = tasks.Settings.from_env({"PX_BUCKET": "b", "AWS_REGION": "eu-west-1", "PX_SECRET_SEC": "a", "PX_SECRET_LLM": "c",
                                 "PX_LOCAL_ROOT": "/scratch/px", "PX_REPO": "/opt/project-x", "PX_PYTHON": "/usr/bin/python3",
                                 "PX_WORKERS": "8", "PX_NPORT_DB": "/data/nport.duckdb"})
    assert s == tasks.Settings("b", "eu-west-1", "a", "c", Path("/scratch/px"), Path("/opt/project-x"), "/usr/bin/python3",
                               8, "/data/nport.duckdb")
    assert (s.secret_id("SEC_USER_AGENT"), s.secret_id("OPENROUTER_API_KEY")) == ("a", "c")


@pytest.mark.parametrize("argv", [
    ["bogus", "--company", CO, "--run-id", RUN],
    ["aliases", "--company", "../etc", "--run-id", RUN],
    ["aliases", "--company", "acme/x", "--run-id", RUN],
    ["aliases", "--company", CO, "--run-id", "a/../b"],
    ["aliases", "--company", CO],
])
def test_cli_rejects_bad_arguments(argv):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 2


def test_cli_runs_the_task_with_settings_from_the_environment(monkeypatch):
    seen = {}
    monkeypatch.setenv("PX_BUCKET", "from-env")
    monkeypatch.setattr(tasks, "client", lambda service, s: f"{service} client")
    monkeypatch.setattr(tasks, "run", lambda *a: seen.setdefault("args", a) and 7)
    previous = signal.getsignal(signal.SIGTERM)
    try:
        assert cli.main(["evidence", "--company", "horizon3_ai", "--run-id", RUN]) == 7
        assert signal.getsignal(signal.SIGTERM) is cli._terminate
    finally:
        signal.signal(signal.SIGTERM, previous)
    task, co, run_id, s, s3, sm = seen["args"]
    assert (task, co, run_id, s.bucket, s3, sm) == ("evidence", "horizon3_ai", RUN, "from-env", "s3 client",
                                                    "secretsmanager client")


def test_sheet_rows_reads_the_workbook_xml(tmp_path):
    make_xlsx(tmp_path / "a.xlsx", {"About": 3, "All evidence": 0, "N-PORT holdings": 5})
    assert tasks.sheet_rows(tmp_path / "a.xlsx") == {"About": 3, "All evidence": 0, "N-PORT holdings": 5}


def test_sheet_rows_matches_openpyxl(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    wb.active.title = "About"
    wb.active["A1"], wb.active["A9"] = "title", "note"
    for name, n in (("All evidence", 7), ("Indirect mentions", 0), ("DOL 401(k)", 2)):
        ws = wb.create_sheet(name)
        ws.append(["#", "Kind"])
        for i in range(n):
            ws.append([i + 1, "row"])
    wb.save(tmp_path / "b.xlsx")
    expected = {ws.title: ws.max_row - 1 for ws in openpyxl.load_workbook(tmp_path / "b.xlsx").worksheets}
    assert tasks.sheet_rows(tmp_path / "b.xlsx") == expected
    assert expected["All evidence"] == 7 and expected["Indirect mentions"] == 0


def test_run_all_counts():
    assert tasks.run_all_counts(["0 of 61 {}", "done {'ok': 60, 'fail': 1}"]) == {"ok": 60, "fail": 1}
    assert tasks.run_all_counts(["done {}"]) == {} and tasks.run_all_counts(["no summary"]) == {}
