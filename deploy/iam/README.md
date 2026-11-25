# IAM policies and Airflow settings

Templates for the AWS side of the deployment. [The operator guide](../README.md) says when to use each one.

| File | What it is | Used for |
|---|---|---|
| `task-role-policy.json` | S3 access of the pipeline image, no secret | every task role (below) |
| `task-secret-sec-policy.json` | reads the SEC user-agent secret | the task role of task 2 (`download`) |
| `task-secret-llm-policy.json` | reads the model-key secret | the task role of tasks 3 and 6 (`finder`, `indirect`) |
| `intake-role-policy.json` | S3 access of the `company_intake` DAG | the AWS identity of the Airflow workers (connection `aws_default`) |
| `task-role-trust-ecs.json` | lets ECS tasks of this account assume a role | the task roles and the task execution role, backend `ecs` only |
| `airflow-variables.example.json` | every Airflow Variable of the contract, with its default or a placeholder | `airflow variables import` |
| `airflow-pools.example.json` | pool `sec_api`, 1 slot | `airflow pools import` |
| `test_iam_files.py` | checks of these files with moto (no AWS account) | `python -m pytest deploy/iam` |

## Placeholders

| Placeholder | Value | In |
|---|---|---|
| `${BUCKET}` | the bucket, `company-reports-data` | task and intake policies |
| `${ACCOUNT_ID}` | the 12-digit account id that owns the bucket and the secrets | task and intake policies, trust policy |
| `${REGION}` | `us-east-2` | trust policy |
| `${SECRET_SEC_ARN}` | full ARN of the SEC user-agent secret (it ends in `-` and six random characters) | `task-secret-sec-policy.json` |
| `${SECRET_LLM_ARN}` | full ARN of the model-key secret | `task-secret-llm-policy.json` |
| `${IMAGE_URI}` | the image in ECR with its tag | Variables |
| `${PX_QUEUE}` | backend `docker`: the Celery queue of the px workers, e.g. `px` (empty: Airflow's default queue; see below) | Variables |
| `${ECS_CLUSTER}`, `${ECS_TASK_DEFINITION}` | cluster name; task definition family (backend `ecs`; empty for `docker`) | Variables |
| `${ECS_SUBNETS}`, `${ECS_SECURITY_GROUPS}` | ids, comma-separated (backend `ecs`, both required; empty for `docker`) | Variables |
| `${ECS_TASK_ROLE_SEC}`, `${ECS_TASK_ROLE_LLM}` | ARNs of the roles `px-task-sec` and `px-task-llm` (backend `ecs`; empty for `docker`) | Variables |
| `${ALERT_EMAILS}` | addresses for failure e-mails, comma-separated (empty: no e-mail) | Variables |

Fill them with the `fill` function of the operator guide; it stops on a placeholder whose shell variable is not set. A
secret ARN may also be given as `arn:aws:secretsmanager:<region>:<account>:secret:px/sec-user-agent-??????`, which
still matches after the secret is deleted and created again.

## Which identity each step uses

| Step | Runs in | Identity (backend `ecs`) | S3 | Secret |
|---|---|---|---|---|
| `company_intake` | Airflow (no image) | intake | lists `input/` and `runs/`; reads and writes `queued` records | none |
| 1 `aliases` | image | `px-task` | reads `input/`; writes `mentions/` | none |
| 2 `download` | image | `px-task-sec` | reads `raw/<co>/sec/`, adds new and changed files | SEC user agent |
| 3 `finder` | image | `px-task-llm` | reads `raw/`, `mentions/input/`; writes `mentions/` | model key |
| 4 `records` | image | `px-task` | reads `reference/`; writes `records/` | none |
| 5 `evidence` | image | `px-task` | reads `raw/`, `mentions/`, `records/`; writes `evidence/`, `prepared/` | none |
| 6 `indirect` | image | `px-task-llm` | reads `prepared/`; writes `indirect/` | model key |
| 7 `publish` | image | `px-task` | reads `raw/` and the run's files; writes `reports/`, `publish/` | none |
| 8 `finalize` | image | `px-task` | the run record only | none |

`mentions/`, `records/` and the other run folders are under `intermediate/<co>/<run_id>/`. Tasks 1–7 also read the
input file, and every task reads and writes the run record `runs/<co>/<run_id>.json`. The intake reads no input file:
the listing gives it each file's ETag.

Where the task role comes from:

- Backend `ecs`: three roles made from `task-role-trust-ecs.json`, each with `task-role-policy.json`: `px-task` (no
  secret), `px-task-sec` (plus `task-secret-sec-policy.json`) and `px-task-llm` (plus `task-secret-llm-policy.json`).
  The task definition's `taskRoleArn` is `px-task`; `company_evidence` runs `download` with a `taskRoleArn` override to
  the Variable `px_ecs_task_role_sec`, and `finder` and `indirect` to `px_ecs_task_role_llm`. So IAM itself keeps each
  secret to its tasks. `aws_default` needs `iam:PassRole` on these three roles and on the task execution role, which
  pulls the image and writes the container log (AWS managed policy `AmazonECSTaskExecutionRolePolicy`).
- Backend `docker`: a container on an Airflow worker gets the worker's instance role through the instance metadata
  service (IMDS). Every other container on that machine, and every task of another DAG that runs there, gets the same
  access, and the px containers get whatever else that role allows (for example what the Airflow worker itself
  needs). So run the px tasks on workers of their own: set the Variable `px_queue`, start the workers for it and for
  `px_large_queue` on machines that run nothing else, and give these policies only to their roles, two of them:
  - the px workers of `px_queue` run every task but `evidence`, so their role needs `task-role-policy.json` and both
    secret policies: each of those tasks can read both secrets, and only the task wrapper keeps it to its own;
  - the evidence machines of `px_large_queue` get a role of their own with `task-role-policy.json` (and ECR pull)
    only: `evidence` reads no secret, and it is the task that parses every filing with pymupdf and lxml, so a crafted
    filing that took over its container finds no secret there.

  Require IMDSv2 on every worker (with IMDSv1 a container reaches IMDS whatever the hop limit); set the hop limit to
  2, which a container on Docker's bridge network needs, only on the px workers and evidence machines, and keep 1 on
  the shared workers, whose role keeps only the intake policy. Docker gives no further isolation between the px
  tasks: use `ecs` where the access model of `ARCHITECTURE.html` must hold.

## Why tasks 2, 3 and 6 read a secret

- **Task 2 (SEC user agent).** It calls SEC EDGAR: full-text search on `efts.sec.gov` and the filings on
  `www.sec.gov`. SEC requires every client to state who it is, with a contact address, in the `User-Agent` header, and
  blocks clients that do not. `tools/sec_http.py` stops when `SEC_USER_AGENT` is not set. The value holds a contact
  e-mail, so it is kept out of the image and the repository like the key.
- **Task 3 (model key).** company-finder runs with `--llm-disambiguate`: each name match, with the company's
  `description`, goes to a model (`openai/gpt-4.1` through OpenRouter) that sets aside look-alike names (another
  organisation with a similar name). company-finder stops with exit code 2 when `OPENROUTER_API_KEY` is not set.
- **Task 6 (model key).** `pipeline/indirect/run_all.py` asks a model (`openai/gpt-4.1`) which company an "it",
  "its" or "they" near a mention refers to, and has a second model (`anthropic/claude-opus-5.5`) confirm the answers
  that name a company, both through OpenRouter (`pipeline/indirect/text_llm.py`). It is skipped when the input has
  `"run_indirect": false`.

No other task calls SEC or a model. The task wrapper reads only the secret of its own task and passes it only to that
task's pipeline process, never to a log or to S3. On `ecs`, IAM also keeps each secret to its tasks (the roles above);
on `docker`, the wrapper is the only barrier.

## Choices in the policies

- **Deletes.** The task role deletes under `reports/<co>/latest/`, where publish replaces the previous workbook, and
  under `intermediate/`: a task cleared and run again removes what its earlier attempt left in its own folders of the
  run, so the next task never reads a filing or table that attempt no longer produces (the runner's `sync_dir`).
  Nothing in `raw/` is deleted (filings are kept across runs), nor in `input/`, `reference/`, the per-run
  `reports/<co>/<run_id>/` or `runs/`; `intermediate/<co>/<run_id>/` keeps each run's latest outputs for audit. A
  role that already has an older version of this policy, without `s3:DeleteObject` on `intermediate/*`, must get the
  current one (`put-role-policy` replaces it). S3 checks a multi-object delete key by key and answers a refused key
  in the response, not with an error; the runner then fails the task (`DeleteFailed ... AccessDenied`) rather than
  leave the earlier attempt's files.
- **Listing.** S3 answers a read of a missing key with 404 only when the caller may list the bucket; otherwise with
  403 AccessDenied. The task wrapper reads keys that may be missing (the run record before it is created, the optional
  N-PORT and DOL welfare files) and treats 404 as "missing", so the task role may list the whole bucket, with no
  `s3:prefix` condition. It can read every prefix of the pipeline's bucket anyway. The intake lists only `input/` and
  `runs/` and reads only records it has just listed, so it does not depend on 404.
- **Records only.** Both roles write `runs/` only as `runs/<co>/<file>.json`. The task role reads `input/` only as
  `input/<co>/company.json`; the intake reads no input file. The intake writes a `queued` record only when it does not
  exist yet (a conditional write, which needs no other permission), so it never replaces a record a task has updated.
- **Account.** Every S3 statement requires `s3:ResourceAccount` = `${ACCOUNT_ID}`, so a bucket of the same name in
  another account is never read or written.
- **Not per company.** Every task role reaches every company's prefixes (`raw/`, `intermediate/`, `reports/`,
  `runs/`): a task of one company that is made to misbehave, for example by a crafted filing, could read or overwrite
  another company's files and records. Limiting a task to its own company needs a design change, so it is not done:
  for example, the task wrapper would assume its role again with a session policy for `*/<co>/*` before it creates
  its clients (the roles trusting themselves), or there would be a role per company.
- **KMS.** Not included. The default bucket encryption (SSE-S3) and the default secret key (`aws/secretsmanager`)
  need nothing more. If the bucket or a secret uses a customer managed KMS key, add `kms:Decrypt` (and
  `kms:GenerateDataKey` for writes to the bucket) on that key.

## Airflow examples

`airflow-variables.example.json` has the 19 Variables listed in `deploy/INTERFACES.md` (section DAGs): the contract's
defaults as written, `${...}` where there is none. Values are strings; lists are comma-separated (a JSON list also
works). `deploy/dags/px_settings.py` reads seven more, optional ones (`px_ecs_container`, `px_ecs_log_group`,
`px_ecs_large_overrides`, `px_task_env`, `px_task_timeout_hours`, `px_run_timeout_hours`, `px_large_slots`); their
defaults fit the setup in the operator guide.

`airflow-pools.example.json` creates `sec_api` with one slot, used by task 2. All SEC calls of this Airflow should
share one pool: if the existing SEC ingestion DAG already has one, set `px_sec_pool` to it instead of creating
`sec_api`.

Checks: `python -m pytest deploy/iam -q` (needs `pytest`, `boto3` and `moto[iam,s3]`).
