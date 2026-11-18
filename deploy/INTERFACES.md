# Build contract: company evidence pipeline on AWS

The design is `deploy/ARCHITECTURE.html` (read it first). This file fixes what the parts share, so they can be built in
parallel and fit together. Repository: `project-x`, branch `aws-deploy`.

## Rules for every part

- The pipeline code is not changed: nothing under `pipeline/`, `tools/`, `company-finder/` is edited. If a part cannot be
  built without a pipeline change, stop and report it.
- Each part writes only in its own folder (table below). No git commits, no branch changes, no pushes.
- Python 3.12. Scratch virtual environments and temporary files go in a temporary folder outside the repository,
  never in the repo.
- Never print, log or copy secrets. `company-finder/.env` holds `OPENROUTER_API_KEY` and `SEC_USER_AGENT`: it must never
  enter the image, a log, S3 or a test fixture. Tests use fake values.
- No real AWS calls. Tests use `moto`. No calls to SEC EDGAR or OpenRouter except where a step below says so.

| Folder | Part |
|---|---|
| `deploy/runner/` | task wrapper `px_runner` (package, tests) |
| `deploy/dags/` | Airflow DAGs `company_evidence`, `company_intake` (+ tests) |
| `deploy/image/` | container image (Dockerfile, Dockerfile.dockerignore) |
| `deploy/iam/`, `deploy/README.md` | IAM policy templates, Airflow variable/pool examples, operator guide |
| `deploy/e2e/` | local end-to-end run on Lungpacer (built after the parts above) |

## Runner: `px_runner`

- Package `deploy/runner/px_runner/`, installable with `pip install deploy/runner` (`deploy/runner/pyproject.toml`,
  distribution name `px-runner`, dependency `boto3`). The pipeline's own dependencies come from
  `company-finder/requirements.txt`.
- Command: `python -m px_runner <task> --company <co> --run-id <run_id>`, where `<task>` is one of
  `aliases download finder records evidence indirect publish finalize`. Exit 0 on success (or a skipped task), non-zero on failure.
  `finalize` exits 0 once it has recorded the run's status, `failed` included, and non-zero only when it could not
  (the DAG takes a run's verdict from the task states, so a retried `finalize` never repeats it).
- Settings (environment variables, all with defaults):

| Variable | Default | Meaning |
|---|---|---|
| `PX_BUCKET` | `company-reports-data` | the S3 bucket |
| `AWS_REGION` | `us-east-2` | region; `AWS_ENDPOINT_URL` (standard boto3) points at moto in tests |
| `PX_SECRET_SEC` | `px/sec-user-agent` | Secrets Manager id; value is the user-agent string, or JSON `{"SEC_USER_AGENT": "..."}` |
| `PX_SECRET_LLM` | `px/openrouter-api-key` | Secrets Manager id; value is the key, or JSON `{"OPENROUTER_API_KEY": "..."}` |
| `PX_LOCAL_ROOT` | `/tmp/px` | local scratch root; wiped at the start of every task (refused when it is a symbolic link, another user's folder or not a folder; re-created with mode 0700) |
| `PX_REPO` | the repo root found from the package location (`/opt/project-x` in the image) | where `pipeline/`, `tools/`, `company-finder/` are |
| `PX_PYTHON` | `sys.executable` | the Python that runs the pipeline scripts |
| `PX_WORKERS` | `4` | `--workers` / `--parallel` for the evidence scan and the indirect step |
| `PX_NPORT_DB` | unset | optional local path to the team N-PORT DuckDB file (15 GB) on the worker's disk |

- Local layout under `PX_LOCAL_ROOT` (`R`), passed to the pipeline as environment variables:
  `PX_DATA=R/data`, `PX_DUMPS=R/data/dumps`, `PX_FINDER_ROOT=R/finder`, `PX_WORK=R/work`. The Form D file goes to
  `R/data/input/formd/formd.duckdb` and is passed as `--formd-db`. Reports are written to `R/reports`.
- Secrets: a task reads only the secret it needs (`download`: SEC; `finder`, `indirect`: LLM), sets it as an environment
  variable for the pipeline subprocess only, and never logs it. On the `ecs` backend IAM enforces this too: each task
  runs as a task role that can read only its secret (DAGs, `deploy/iam/`). Subprocess output is streamed to stdout; on failure the
  last 40 lines go into the run record (strip anything that looks like a key first).
- Config: `input/<co>/company.json` is the pipeline config shape (see ARCHITECTURE section 4) plus `run_indirect`
  (default `true`). The runner writes it to `<PX_REPO>/pipeline/config/<co>.json` (the pipeline reads its config from
  there; inside a container that copy is throwaway). The company name used for the workbook (`<Name>`) is
  `company.legal_name`, else `name`. `--cik` is `company.cik`.

### S3 keys (exact)

| S3 key | Local path |
|---|---|
| `input/<co>/company.json` | `<PX_REPO>/pipeline/config/<co>.json` |
| `raw/<co>/sec/manifest.csv` | `R/data/dumps/<co>_manifest.csv` |
| `raw/<co>/sec/files/<path>` | `R/data/dumps/<co>/<path>` |
| `intermediate/<co>/<run_id>/mentions/<path>` | `R/finder/<path>` (`input/<co>_input.csv`, `out/<co>/<co>.csv`, `<co>_mentions.csv`, `<co>_dropped.csv`, `<co>_summary.csv`) |
| `intermediate/<co>/<run_id>/records/inputs/<path>` | `R/work/inputs/<co>/<path>` |
| `intermediate/<co>/<run_id>/records/formd_offerings.csv` | `R/work/out/<co>/formd_offerings.csv` |
| `intermediate/<co>/<run_id>/records/nport/<path>` | `R/data/dumps/<co>_nport/<path>` |
| `intermediate/<co>/<run_id>/evidence/<path>` | `R/work/out/<co>/evidence/<path>` |
| `intermediate/<co>/<run_id>/prepared/<path>` | `R/work/prepared/<path>` (`companies/`, `pdfs/`) |
| `intermediate/<co>/<run_id>/indirect/<path>` | `R/work/out/<co>/indirect/<path>` |
| `intermediate/<co>/<run_id>/publish/indirect_located.csv` | `R/work/out/<co>/indirect_located.csv` |
| `reports/<co>/<run_id>/<file>.xlsx` and `reports/<co>/latest/<file>.xlsx` | `R/reports/<file>.xlsx` |
| `runs/<co>/<run_id>.json` | run record (below) |
| `reference/formd/formd.duckdb` | `R/data/input/formd/formd.duckdb` |
| `reference/nport/<co>/nport_holdings.csv` (optional) | `R/data/dumps/<co>_nport/nport_holdings.csv` |
| `reference/dol/raw/form5500_rows.csv`, `form5500sf_rows.csv`, `schedule_h_rows.csv` | `R/data/dumps/employee_docs/raw/` |
| `reference/dol/data/welfare_plans_by_year.csv` (optional) | `R/data/dumps/employee_docs/data/` |

Only the company's main dump folder (`<co>`) is supported in this phase; `<co>_ncsr` / `<co>_delta` are a later step.

### Tasks (all commands run with `cwd=<PX_REPO>` and the `PX_*` environment above)

1. `aliases`: `tools/make_companies_file.py <co> --config <cfg> --force` (writes `R/finder/input/<co>_input.csv`); upload `mentions/`.
2. `download` (SEC secret; Airflow pool `sec_api`): stage `raw/<co>/sec/`; `tools/download_filings.py <co> --config <cfg> --dumps R/data/dumps`; upload the manifest and every file that is new or changed (compare size; never delete in `raw/`).
3. `finder` (LLM secret): stage `raw/` and `mentions/input/`; `company-finder/company_finder.py --companies R/finder/input/<co>_input.csv --input R/data/dumps/<co> --out R/finder/out/<co>/<co>.csv --format csv --llm-disambiguate`. The `--out` name must be `<co>.csv`: then the finder writes `<co>_mentions.csv` (one row per mention), the file `prepare_filings.py` reads. Upload `mentions/`.
4. `records`: stage `reference/` (Form D file, DOL rows, N-PORT file if present). N-PORT: if `PX_NPORT_DB` is set, run `pipeline/sources/nport.py export-db` then `nport.py build` into `R/data/dumps/<co>_nport/` (read `nport.py` for the exact options); else use `reference/nport/<co>/nport_holdings.csv` if it exists; else write no N-PORT file. Then `pipeline/sources/records.py <co> --cik <cik> --formd-db R/data/input/formd/formd.duckdb --employee-dir R/data/dumps/employee_docs/raw`. A missing optional source (DOL welfare file, N-PORT) is reported by `records.py` itself and is not a task failure; a non-zero exit is. Upload `records/`.
5. `evidence` (Airflow queue `evidence_large`): stage `raw/`, `mentions/`, `records/`; then, in order: `pipeline/evidence/evidence_tables.py <co> --folders <co> --workers <PX_WORKERS>`, `evidence_build.py <co>`, `evidence_holdings.py <co>`, `validate_all.py <co>`, `evidence_holdings.py <co> --use-validation`, `evidence_facts.py <co> <Name>`, `locate.py <co>` (all in `pipeline/evidence/`), then `pipeline/prepare_filings.py <co>` with no `--link` (copies, so another machine can read them). Upload `evidence/` and `prepared/`.
6. `indirect` (LLM secret): if `run_indirect` is false, record `skipped` and exit 0. Else stage `prepared/` and `indirect/` (a filing an earlier attempt finished is kept, unless its model calls failed, its files are incomplete, or it was made from other prepared files: the runner's `<stem>_indirect_input.json` holds the SHA-256 of the filing's `.meta.json`, mentions file and document it was made from); `pipeline/indirect/run_all.py <co> R/work/out/<co>/indirect --parallel <PX_WORKERS>`; upload `indirect/` (also after a failure or a stop: the CSV files first, then the run files, then the input files, then the deletes); record the model cost: the sum of `cost_usd` over the calls of every `*_indirect_run.json` this run's task 6 produced, also those a retry replaced (`indirect/model_cost.json`).
7. `publish`: stage `raw/`, `records/`, `evidence/`, `prepared/` and `indirect/` (if task 6 ran); if `indirect/` exists, `pipeline/indirect/locate_mentions.py <co> R/work/out/<co>/indirect`; then `pipeline/findings_excel.py <co> <Name> R/reports`. Upload the workbook to `reports/<co>/<run_id>/` and replace `reports/<co>/latest/`; upload `publish/indirect_located.csv`; record the workbook key and the row count of each sheet.
8. `finalize` (always runs, Airflow `trigger_rule=all_done`): overall `status` = `succeeded` if tasks 1–7 all `succeeded` or `skipped`, else `failed`; set `ended_at`; exit 0 in both cases.

### Run record `runs/<co>/<run_id>.json`

```json
{
  "company": "lungpacer", "run_id": "20261117T101500Z-1a2b3c4d",
  "status": "queued | running | succeeded | failed",
  "input_etag": "<ETag of input/<co>/company.json when the run was planned>",
  "created_at": "2026-11-17T10:15:00Z", "started_at": "...", "ended_at": "...",
  "tasks": {
    "<task>": {"status": "running | succeeded | failed | skipped", "started_at": "...", "ended_at": "...",
               "seconds": 12.3, "outputs": {"...": "counts, keys, model_cost_usd"}, "error": "last lines on failure"}
  },
  "model_cost_usd": {"finder": null, "indirect": 0.3449},
  "workbook": "reports/<co>/<run_id>/<file>.xlsx"
}
```

The intake DAG creates the record (`queued`, `input_etag`, `created_at`); the first task sets `running` and `started_at`
(and creates the record if it is missing). Every task updates only its own entry under `tasks` (read, update, write: tasks
of one run never overlap). Times are UTC ISO 8601 with `Z`.

## DAGs

- `deploy/dags/px_settings.py`: Airflow Variables with defaults: `px_bucket` (`company-reports-data`), `px_region`
  (`us-east-2`), `px_image` (ECR image URI), `px_task_backend` (`docker` or `ecs`, default `docker`), `px_docker_url`
  (`unix://var/run/docker.sock`), `px_large_queue` (`evidence_large`), `px_queue` (none: Airflow's default queue),
  `px_ecs_cluster`, `px_ecs_task_definition`, `px_ecs_subnets`, `px_ecs_security_groups`, `px_ecs_task_role_sec`,
  `px_ecs_task_role_llm` (role ARNs; these six are required for ecs), `px_alert_emails`, `px_refresh_days` (`7`),
  `px_stale_hours` (`24`), `px_sec_pool` (`sec_api`), `px_secret_sec`, `px_secret_llm`.
- `company_evidence`: no schedule; params `company` and `run_id` (from `dag_run.conf`; every task fails at once when
  the conf's `run_id` is not the Airflow run id); tasks `aliases >> download >> finder >> records >> evidence >> indirect
  >> publish`, then `finalize` (`trigger_rule=all_done`; it fails the DAG run when one of tasks 1–7 did not succeed).
  Each task runs the image with `python -m px_runner <task> --company … --run-id …` and the `PX_*` settings as environment.
  `download` in pool `sec_api`; `evidence` on the large queue (docker backend, at most `px_large_slots` at once) or with
  a larger CPU/memory override (ecs backend). Retries 2, 5-minute delay (`finalize` too: its verdict on a failed run is
  never retried); each try is stopped after `px_task_timeout_hours`, the DAG run after `px_run_timeout_hours`. Failure
  e-mail to `px_alert_emails` from a failed task (none from `finalize` for a failed run) and from the DAG for a run that
  timed out. `max_active_runs` 4. Backend `docker`: `DockerOperator`, the tasks other than `evidence` on `px_queue`; a
  task first stops and removes the containers its earlier tries left on that Docker daemon and removes the exited
  containers of the company's earlier runs there, and ends its own with `docker stop` before removing it, also on a
  timeout (so the runner records the stop). A try that Airflow stops from outside (the DAG run's timeout, the task
  marked failed) is killed 5 seconds later, before it removes its container, which stays exited until a task of the
  company's next run on that Docker daemon removes it. Backend
  `ecs`: `EcsRunTaskOperator` (never deferred) with a container command override; `download` runs with `taskRoleArn` =
  `px_ecs_task_role_sec`, `finder` and `indirect` with `px_ecs_task_role_llm`, the others as the task definition's role
  (no secret).
- `company_intake`: every 15 minutes, `catchup=False`, `max_active_runs=1`. It lists `input/*/company.json` (with ETag)
  and the latest run record of each company, and starts a run when: no run exists; or the input ETag differs from the
  latest run's `input_etag`; or the latest successful run ended more than `px_refresh_days` ago. It never starts a run for
  a company whose latest record is `queued` or `running` and younger than `px_stale_hours`, or that Airflow still
  reports as queued or running; when Airflow cannot answer, it plans nothing (the task fails and is retried). For each
  run it writes the `queued` record (a conditional write: an existing record is never replaced), then triggers
  `company_evidence` with `conf={"company": co, "run_id": run_id}` and the same run id. A failed intake run is e-mailed
  by the DAG's own callback. `run_id` = `<UTC time YYYYMMDDTHHMMSSZ>-<first 8 characters of the input ETag>`. The
  decision is a pure function `decide(inputs, latest_records, now, refresh_days, stale_hours)` tested without Airflow.
- Optional Variables, with defaults in `px_settings.py`: `px_task_timeout_hours` (12), `px_run_timeout_hours` (72),
  `px_large_slots` (1), `px_task_env` (extra non-secret environment), `px_ecs_container`, `px_ecs_log_group`,
  `px_ecs_large_overrides`.
