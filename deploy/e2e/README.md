# Local end-to-end check (Lungpacer)

`run_e2e.py` runs the whole deployment on one machine. It builds the pipeline image, starts a local moto server
standing in for S3 and Secrets Manager, and seeds the bucket with Lungpacer's input file, filings and reference data.
Then it runs every task of `company_evidence` in a fresh container, in the DAG's order and with the DAG's command
(`python -m px_runner <task> --company lungpacer --run-id <run_id>`). Last, it checks the run record and every S3 key
of [INTERFACES.md](../INTERFACES.md), and compares the workbook with one built from the same filings by the main
branch's code.

Nothing here touches AWS: the script and the containers get dummy credentials and the moto endpoint, and the script
switches off the AWS config and credentials files. The `finder` and `indirect` tasks call OpenRouter for real (about
0.35 USD for Lungpacer), with the real key: see [Secrets and the moto server](#secrets-and-the-moto-server) for how it
is handled. The `download` task is not run, so there is no SEC traffic: the filings are seeded from the local dump.

| File | What it is |
|---|---|
| `run_e2e.py` | the check: steps `build`, `moto`, `seed`, `run`, `check`, `compare`, `stop` |
| `compare_workbooks.py` | compares two findings workbooks: cell values and hyperlink targets (also on its own) |
| `build_expected.py` | builds the expected workbook: main's code on this machine, same filings, given model outputs |
| `requirements.txt` | moto server, boto3 and openpyxl for the script's own virtual environment |

## Before you start

- Docker running. On Docker Desktop (macOS) containers reach the host's `127.0.0.1` at `host.docker.internal`. On
  Linux the script adds `--add-host host.docker.internal:host-gateway`, and moto must listen where containers can
  reach it: pass `--moto-host` with the `docker0` gateway address
  (`docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}'`, usually `172.17.0.1`). Never a
  wildcard such as `0.0.0.0`, which the script refuses: moto has no authentication.
- Python 3.12 and a scratch virtual environment for the script (not the repository's):

  ```sh
  W=/tmp/px-e2e                                   # any scratch folder outside the repository
  python3.12 -m venv $W/venv
  $W/venv/bin/pip install -r deploy/e2e/requirements.txt
  ```

- `company-finder/.env` with `OPENROUTER_API_KEY` (the `finder` and `indirect` tasks read it) and, for the secret
  scan of `check` only, `SEC_USER_AGENT`. The script reads only those two lines and never prints the values or
  writes them to a file.
- The data below. The defaults are this machine's paths; each has an option. Files are copied into moto, and the
  originals are only read.

| Option | Default | Seeded as |
|---|---|---|
| `--config` | `pipeline/config/lungpacer.json` | `input/lungpacer/company.json`, plus `run_indirect: true` and the two fields below |
| `--companies` | `Resources/Targeted_pdf/port_v2_run/input/lungpacer_input.csv` | `company.legal_name` and `company.description` (columns `canonical_name`, `description`): the finder's look-alike check needs them |
| `--manifest` | `../dumps/lungpacer_manifest.csv` | `raw/lungpacer/sec/manifest.csv` |
| `--files` | `../dumps/lungpacer/` (symlinks followed) | `raw/lungpacer/sec/files/` |
| `--formd` | `~/projectx_test_archive/runs/lungpacer/input/formd/formd.duckdb` | `reference/formd/formd.duckdb` |
| `--dol` | `../dumps/employee_docs/raw/` | `reference/dol/raw/` (3 files) |
| `--nport` | `../dumps/lungpacer_nport/nport_holdings.csv` | `reference/nport/lungpacer/nport_holdings.csv` |
| `--env` | `company-finder/.env` | not seeded: `run` stores `px/openrouter-api-key` only while `finder` or `indirect` runs |

## Run

From the repository root:

```sh
$W/venv/bin/python deploy/e2e/run_e2e.py --work $W/work --expected <expected workbook>
```

The moto server stops when that command ends, and its data (in memory) goes with it. Add `--keep-moto` to keep it,
with the bucket and no secret, for a later invocation: to continue a failed run (below) or for
`build_expected.py --from-run`. Then stop it when done:

```sh
$W/venv/bin/python deploy/e2e/run_e2e.py --work $W/work --steps stop
```

| Step | Does |
|---|---|
| `build` | `docker build -f deploy/image/Dockerfile -t px-pipeline:dev <repo>` (`--image` for another tag) |
| `moto` | starts `moto_server` on `127.0.0.1:5055` (`--moto-host`, `--port`) with CORS off, detached from the terminal. A server kept by an earlier `--keep-moto` invocation is reused, with its data |
| `seed` | creates the bucket `company-reports-data` and uploads the input file, raw filings and reference files. Uploads only what is missing or a different size. No secret |
| `run` | plans a run as `company_intake` does, with `deploy/dags/px_intake.py`: the decision, the run id `<UTC time>-<ETag[:8]>` and the `queued` record. Then one `docker run --rm` per task: `aliases`, `finder`, `records`, `evidence`, `indirect`, `publish`, then `finalize`. `download` is recorded as `skipped`, with the reason, the way the runner records a skipped task. The model key is in moto only while `finder` or `indirect` runs |
| `check` | the run record and the S3 keys (below) |
| `compare` | downloads the workbook, checks that `latest/` holds the same file, and compares it with `--expected` |
| `stop` | stops the moto server the script started (also one kept with `--keep-moto`) |

Each container gets `AWS_ENDPOINT_URL=http://host.docker.internal:<port>`, `AWS_REGION=us-east-2`, dummy
`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`, and `PX_BUCKET`. Everything else uses the runner's defaults
(`PX_WORKERS` 4; `--workers` sets it). Per run, `$W/work/runs/<run_id>/` holds each task's full log, `timings.json`,
`run_record.json`, `check.json`, the workbook and the comparison (`compare.txt`, `compare.json`). `--quiet` shows
only the runner's own lines on the console.

**When a task fails**, the script prints the error from the run record and runs `finalize` (the DAG's `all_done`),
so the run ends `failed` (finalize records that and exits 0; the script reads the record). Continuing that run needs
its data in moto, so only a run started with `--keep-moto` can be continued; without it, moto stops and the next run
starts again. With `--keep-moto` the script prints the command to continue: fix the cause, then

```sh
$W/venv/bin/python deploy/e2e/run_e2e.py --work $W/work --steps build,run,check,compare \
    --run-id <run_id> --from-task <task> --expected <expected workbook> --keep-moto
```

The tasks run again with the same run id, as a cleared Airflow task would. `indirect` skips the filings an earlier
attempt finished, so a retry pays nothing for them.

## What `check` verifies

- **Run record.** `status` is `succeeded`, and `created_at`, `started_at` and `ended_at` are UTC with `Z`.
  `input_etag` is the input file's ETag, and the run id ends with its first 8 characters. Every task `succeeded`, with
  no `error`, except `download`, `skipped` here. `model_cost_usd.indirect` is set and equals the task's own output.
  `workbook` is under `reports/lungpacer/<run_id>/`.
- **S3 keys.** Every key of the contract's table exists:
  - the input file and the raw manifest;
  - `mentions/`: the companies file and the finder's `.csv`, `_mentions`, `_dropped` and `_summary`;
  - `records/`: Form D offerings, DOL rows, 401(k) participants, N-PORT price marks, N-PORT holdings;
  - `evidence/`: the files the workbook reads;
  - `indirect/run.log` and `publish/indirect_located.csv`;
  - the workbook under `<run_id>/` and `latest/`;
  - the run record and the reference files.
- **Counts.** The raw files are as seeded. There is one prepared filing per mentions file, per copied filing and per
  `*_indirect_run.json`, matching the counts in the record, and one `*_indirect_input.json` per run file. No filing
  failed in `indirect`, and `indirect/model_cost.json` adds up to the recorded model cost. `latest/` holds only this
  run's workbook (same ETag). Every task uploaded files.
- **Secrets.** No secret is left in the moto server. The two values from `.env`, raw and URL-encoded, appear in no
  S3 object, in no log under `--work` (task, build and moto logs), and nowhere in the image: every file of a
  `docker export`, plus the image config and history. `--no-image-scan` skips the image. Only counts are printed.

## Secrets and the moto server

moto has no authentication: any process that reaches its port can read every secret it holds (for example at
`/moto-api/data.json`). So:

- **Only the model key goes into moto, and only while a task needs it.** `run` reads `OPENROUTER_API_KEY` from
  `company-finder/.env` right before the `finder` container starts, stores it as `px/openrouter-api-key`, and deletes
  it (no recovery window) as soon as that container ends, also when the task fails or the script is interrupted; the
  same for `indirect`. `SEC_USER_AGENT` never goes into moto: `download` is not run. `seed` stores no secret, and
  `check` fails if one is left.
- **moto listens on one address**: `127.0.0.1` by default, the `docker0` gateway on Linux, never a wildcard. It runs
  with CORS off (`MOTO_DISABLE_GLOBAL_CORS=true`), which stops a web page of another origin from reading its
  answers. It does not stop DNS rebinding (moto answers any `Host` header, so a page that points its own host name at
  `127.0.0.1` reads moto as its own origin, in a browser that does not block such requests), nor local processes or
  containers. What remains exposed that way is the model key while `finder` or `indirect` runs (above).
- **moto stops when the invocation ends**: after the last step, a failure, an exception, Ctrl-C, SIGTERM or SIGHUP.
  Only `--keep-moto` keeps it running, with the bucket and no secret. A script killed with SIGKILL cannot stop it:
  run `--steps stop` then.
- **The values are never printed, logged or written to a file.** `check` reads both from `.env` only to look for them
  in what the run wrote.

## Expected workbook

`build_expected.py` builds the workbook the run is compared with. It uses the committed code of `--ref` (default
`main`, `git archive`), on this machine with the pipeline's own Python (`company-finder/.venv`). It reads copies of
the same filings and reference data, plus the model outputs of an earlier run: company-finder's kept and dropped
mentions and the indirect step's answers. No model is called: there is no key in its environment and no `.env` in
its copy of the code. It runs `run_company.py` (no `--indirect`), `locate_mentions.py` and `findings_excel.py`.

```sh
# the current workbook: main's code with the model outputs of the local run in work/ (15 November 2026)
$W/venv/bin/python deploy/e2e/build_expected.py --out $W/expected_main
# main's code with the model outputs of an end-to-end run: any difference from that run comes from the deployment.
# It reads them from that run's moto server: run run_e2e.py with --keep-moto, and stop moto afterwards (--steps stop)
$W/venv/bin/python deploy/e2e/build_expected.py --out $W/local_same_models --from-run <run_id>
```

With `--from-run`, a key of that run that would land outside `--out` (an absolute path or `..`) stops the script.

`compare_workbooks.py EXPECTED GOT` compares two workbooks:

- All evidence, Form D offerings, DOL 401(k), Holdings (filings) and N-PORT holdings, cell for cell: the value and
  the hyperlink (target and location). When cells differ, rows are matched too, which tells moved rows from changed
  ones.
- Indirect mentions, row by row.
- About, cell for cell, except the indirect count "`<n>` places found; `<m>` sit in a paragraph", which depends on
  the model run.

## Result, 17 November 2026

This run predates the secret handling above (18 November): the seed step then stored both secrets, moto kept
running between invocations (so the retry below continued the run without `--keep-moto`), and `check` had no
"no secret left in the moto server" item.

Run `20261117T165805Z-be6b3c54`, image `px-pipeline:dev` (linux/arm64, Docker Desktop on Apple silicon). Every task
succeeded on the first attempt. Total model cost: 0.3419 USD (indirect). The finder's cost is not recorded
(`null`): company-finder does not report it.

| Task | Status | Seconds (record) | Seconds (with container start) |
|---|---|---|---|
| aliases | succeeded | 0.0 | 0.4 |
| download | skipped (not run here) | 0.0 | |
| finder | succeeded | 99.3 | 99.7 |
| records | succeeded | 0.2 | 0.6 |
| evidence | succeeded | 18.8 | 19.3 |
| indirect | succeeded | 60.4 | 61.0 |
| publish | succeeded | 13.6 | 14.1 |
| finalize | run `succeeded` | | 0.4 |

- **check:** 61 of 61 passed. That includes no secret in the 565 S3 objects (776 MB), the 11 logs or the image's
  17,402 files.
- **compare**, against main's code with the model outputs of the local run of 15 November: all seven sheets are
  identical cell for cell, values and links, and in the same order. The About counts are equal too (1 place found,
  1 in a paragraph that names the company). Unzipped, the two files differ only in `docProps/core.xml`, the creation
  time. Lungpacer exercises All evidence (57 rows), Form D offerings (7), DOL 401(k) (9) and About; Holdings
  (filings), N-PORT holdings and Indirect mentions have no rows for this company.
- **Model run against 15 November:**
  - The finder kept and dropped the same mentions (92 and 23), with the same reasons.
  - The indirect step found the same 86,221 trigger words and sent 255 to the model. 253 got the same decision.
    Two, in a 10-K (0001104659-21-120762), changed from `none` to `no_answer`; neither resolves to Lungpacer.
  - In both runs, one place resolves to the company, in a paragraph that names it, so the Indirect mentions sheet is
    empty both times.
  - Confidence was the same for 204 of the 255 and the reason text for 178: model wording varies, as expected.
- **Deployment only.** `build_expected.py --from-run` ran main's code on this run's own model outputs. Compared file
  by file with what the containers wrote to S3, 200 files are byte-identical. Two kinds differ, neither reaching the
  workbook:
  - `evidence/prose_sentences.csv`: the same rows in another order. `evidence_tables.py` writes rows as its worker
    pool finishes them (`imap_unordered`), and `findings_excel.py` sorts sentences itself.
  - The 80 files in `evidence/table_cache/`: only the absolute filing path they record (`/tmp/px/...` in the
    container).
- **Failure and retry.** With `reference/formd/formd.duckdb` removed, `records` failed with
  `missing s3://company-reports-data/reference/formd/formd.duckdb`, and `finalize` set the run to `failed`. After
  `seed` restored the file, `--from-task records` re-ran records, evidence, indirect and publish under the same run
  id:
  - `indirect` skipped all 61 finished filings: 2.4 s, no new model cost, recorded cost unchanged.
  - `finalize` set the run back to `succeeded`.
  - `check` passed 60 of 60 (image scan skipped) and the comparison was again identical.

  In that retry, `publish` took 72 s instead of 14. One 8 MB ranged GET of a 23 MB filing stalled for 60 s, botocore's
  read timeout, and was retried. This is the local moto server and Docker Desktop's port forwarding, not the task.

## What this check does not cover

- **`download`** (SEC full-text search and archives, the SEC user-agent secret) is not run. The runner's unit tests
  cover it with a stand-in pipeline.
- **Airflow is not in the loop.** The script starts the containers itself, in the DAG's order, with its command and
  environment. Retries, the `sec_api` pool, the large queue, `max_active_runs`, the e-mails and the
  `company_intake` schedule are covered by `deploy/dags/tests`.
- **moto is not AWS.** IAM policies, ECR, ECS and the instance-profile credentials in containers are not exercised.
- **Image architecture.** The image here is linux/arm64; production builds `--platform linux/amd64` (see
  `deploy/README.md`).
- **Lungpacer is a small case.** One dump folder, no N-PORT holdings, no holdings rows in fund filings, and no DOL
  welfare file (`records` lists `DOL Form 5500` under `not_built`, as the local run does). A company with holdings and
  N-PORT rows, such as Horizon3, would exercise the holdings and N-PORT paths of the evidence and publish tasks.
