# Company evidence pipeline on AWS: operator guide

The files in this folder run the company evidence pipeline (`pipeline/`, `tools/`, `company-finder/`) on AWS under
Airflow. An analyst puts one input file per company in S3. The `company_intake` DAG sees it and starts a `company_evidence` run,
which downloads the company's SEC filings, builds the evidence and writes the findings workbook to the company's
folder in S3. The pipeline code is not changed: each task copies its inputs from S3 to local disk, runs the existing
script and copies the outputs back.

- Design: [ARCHITECTURE.html](ARCHITECTURE.html)
- Build contract (S3 keys, task commands, run record, DAG rules): [INTERFACES.md](INTERFACES.md)

| Folder | Holds |
|---|---|
| `runner/` | `px_runner`, the task wrapper: `python -m px_runner <task> --company <co> --run-id <run_id>` |
| `image/` | the container image (`Dockerfile`, `Dockerfile.dockerignore`, `constraints.txt`) |
| `dags/` | the DAGs `company_intake` and `company_evidence` and their settings (`px_settings.py`) |
| `iam/` | IAM policy templates and the Airflow Variables and pool examples ([iam/README.md](iam/README.md)) |
| `e2e/` | the local end-to-end check on Lungpacer |

Contents: [1 Before you start](#1-before-you-start) · [2 Shell settings](#2-shell-settings) ·
[3 Bucket and reference data](#3-bucket-and-reference-data) · [4 Secrets](#4-secrets) · [5 IAM](#5-iam) ·
[6 Image](#6-image) · [7 Where tasks run](#7-where-tasks-run) · [8 DAGs, Variables and pool](#8-dags-variables-and-pool) ·
[9 Input file](#9-input-file) · [10 S3 layout](#10-s3-layout) · [11 Run record](#11-run-record) ·
[12 Start and re-run a company](#12-start-and-re-run-a-company) · [13 Local end-to-end check](#13-local-end-to-end-check) ·
[14 Known limits](#14-known-limits)

## 1. Before you start

- An AWS account, region `us-east-2`, the AWS CLI v2, and rights to create S3, ECR, Secrets Manager and IAM resources.
- The existing Airflow 3.2 deployment with the providers `apache-airflow-providers-amazon` (S3, ECS),
  `apache-airflow-providers-docker` (backend `docker`), `apache-airflow-providers-smtp` (failure e-mail) and
  `apache-airflow-providers-standard` (installed with Airflow 3), and two connections: `aws_default` (the AWS
  identity of the intake DAG and of the ECS operator; with no credentials in it, the worker's instance profile is
  used) and `smtp_default` (SMTP server and `from_email`).
- Where the tasks run, one of (section 7):
  - `docker`, the default: Airflow workers of their own with Docker, plus one large worker for the evidence task;
    every task there runs with the AWS role of its worker machine;
  - `ecs`: an ECS cluster; each task runs as a role that can read only the secret it needs.
- Outbound HTTPS from the tasks to S3, Secrets Manager and ECR, to `efts.sec.gov` and `www.sec.gov` (task 2) and to
  `openrouter.ai` (tasks 3 and 6).
- On the build machine: Docker 23 or later (BuildKit is the default builder) and a checkout of the repository.

## 2. Shell settings

Run the commands of this guide from the repository root, in one shell with these variables. `fill` writes a template
with its `${NAME}` placeholders replaced by the shell variables of the same name, and stops if one is not set.

```sh
export ACCOUNT_ID=123456789012                 # your account
export REGION=us-east-2 AWS_REGION=us-east-2
export BUCKET=company-reports-data
export ECR_REPO=px-pipeline
OUT=$(mktemp -d)                               # filled templates go here, not into the repository
fill() { python3 -c 'import os, string, sys; sys.stdout.write(string.Template(open(sys.argv[1]).read()).substitute(os.environ))' "$1"; }
```

## 3. Bucket and reference data

Create the bucket if it does not exist. New buckets block public access and encrypt objects (SSE-S3) by default;
keep both.

```sh
aws s3api create-bucket --bucket $BUCKET --create-bucket-configuration LocationConstraint=$REGION
```

Task 4 reads reference data that is shared by every company and maintained outside this deployment:

| S3 key | What | Where it comes from |
|---|---|---|
| `reference/formd/formd.duckdb` | Form D database (tables `companies`, `filings`) | the team's Form D database |
| `reference/dol/raw/form5500_rows.csv`, `form5500sf_rows.csv`, `schedule_h_rows.csv` | DOL Form 5500 rows; column `company` holds the input's `company.employee_company` | the DOL ingestion job |
| `reference/dol/data/welfare_plans_by_year.csv` | optional; not produced today (section 14) | none yet |
| `reference/nport/<co>/nport_holdings.csv` | optional; the company's N-PORT holdings | built as below |

```sh
aws s3 cp formd.duckdb s3://$BUCKET/reference/formd/formd.duckdb
aws s3 cp --recursive employee_docs/raw/ s3://$BUCKET/reference/dol/raw/
```

The company's CIK must be in the Form D file, or task 4 stops with `CIK ... is not in ...` and the run fails.
`tools/build_formd_db.py <co>` builds a stand-in file for one company (and the SPVs whose notices name it); uploading it
replaces the file for every company.

To build a company's N-PORT file where the team N-PORT database is (15 GB), with the repository's environment:

```sh
python pipeline/sources/nport.py export-db --dump /tmp/nport/lungpacer_nport --db <path>/nport_team.duckdb \
    --config pipeline/config/lungpacer.json
python pipeline/sources/nport.py build --dump /tmp/nport/lungpacer_nport \
    --out /tmp/nport/lungpacer_nport/nport_holdings.csv --config pipeline/config/lungpacer.json
aws s3 cp /tmp/nport/lungpacer_nport/nport_holdings.csv s3://$BUCKET/reference/nport/lungpacer/nport_holdings.csv
```

`--config` is the company's input file (the same JSON as `input/<co>/company.json`); its `company.name_regex` selects
the holdings. The file is a snapshot: build it again when the database changes.

## 4. Secrets

Two secrets in Secrets Manager. A task reads only its own secret at start and passes it only to its pipeline process.

| Secret id | Value | Read by |
|---|---|---|
| `px/sec-user-agent` | the SEC user agent: organisation and contact e-mail, e.g. `your-org-research contact@your-domain` | task 2 |
| `px/openrouter-api-key` | the OpenRouter API key | tasks 3 and 6 |

The value is the plain string, or JSON `{"SEC_USER_AGENT": "..."}` / `{"OPENROUTER_API_KEY": "..."}`. Type the values;
do not copy `company-finder/.env` anywhere. The commands below keep the values out of the shell history and the
process list:

```sh
read -rs VALUE                # paste the SEC user agent, then Enter (nothing is shown)
printf '%s' "$VALUE" | aws secretsmanager create-secret --name px/sec-user-agent --secret-string file:///dev/stdin
read -rs VALUE                # paste the OpenRouter key
printf '%s' "$VALUE" | aws secretsmanager create-secret --name px/openrouter-api-key --secret-string file:///dev/stdin
unset VALUE
export SECRET_SEC_ARN=$(aws secretsmanager describe-secret --secret-id px/sec-user-agent --query ARN --output text)
export SECRET_LLM_ARN=$(aws secretsmanager describe-secret --secret-id px/openrouter-api-key --query ARN --output text)
```

To change a value later, use `put-secret-value` the same way; the next task that starts reads it. Give this
deployment its own OpenRouter key with a credit limit set in OpenRouter. When the limit is reached, the model calls
fail and so does the task (task 6 records the filings whose calls failed); after topping up, clear the task and it
runs those filings again. With other secret names, set the Variables `px_secret_sec` and `px_secret_llm`.

## 5. IAM

The templates and the reasons for each permission are in [iam/README.md](iam/README.md).

```sh
fill deploy/iam/task-role-policy.json       > $OUT/task-role-policy.json        # S3, every task
fill deploy/iam/task-secret-sec-policy.json > $OUT/task-secret-sec-policy.json  # SEC user agent, task 2
fill deploy/iam/task-secret-llm-policy.json > $OUT/task-secret-llm-policy.json  # model key, tasks 3 and 6
fill deploy/iam/intake-role-policy.json     > $OUT/intake-role-policy.json
```

| Identity | Gets |
|---|---|
| Backend `ecs`: task role `px-task` (tasks 1, 4, 5, 7 and `finalize`) | `task-role-policy.json` |
| Backend `ecs`: task role `px-task-sec` (task 2) | `task-role-policy.json`, `task-secret-sec-policy.json` |
| Backend `ecs`: task role `px-task-llm` (tasks 3 and 6) | `task-role-policy.json`, `task-secret-llm-policy.json` |
| Backend `docker`: the instance role of the px workers of `px_queue` (every task but evidence, section 7) | `task-role-policy.json`, both secret policies, ECR pull (`AmazonEC2ContainerRegistryReadOnly`) |
| Backend `docker`: the instance role of the evidence machines (`px_large_queue`), a role of their own | `task-role-policy.json`, ECR pull; no secret (evidence reads none, and it parses every filing) |
| `aws_default` (the intake DAG) | `intake-role-policy.json` |
| `aws_default`, backend `ecs` | `ecs:RunTask` on the task definition; `ecs:DescribeTasks`, `ecs:ListTasks` and `ecs:StopTask` in the cluster (the operator finds a task it started before a worker restart); `iam:PassRole` on `px-task`, `px-task-sec`, `px-task-llm` and `px-task-execution` (condition `iam:PassedToService` = `ecs-tasks.amazonaws.com`); `logs:GetLogEvents` on the log group `/ecs/px-pipeline` |

Every task role can reach every company's prefixes in S3 (section 14). A task that runs again deletes what its
earlier attempt left in its own folders under `intermediate/`, so a role that already has an older
`task-role-policy.json` (one without `s3:DeleteObject` on `intermediate/*`) must get the current one: `put-role-policy`
below replaces it. Otherwise such a task fails with `DeleteFailed ... AccessDenied` rather than leave those files.

Backend `docker`: a container uses the instance role of the worker machine it runs on, so the tasks run on workers
of their own (section 7) and only their roles get the task policies. The px workers of `px_queue` run every task but
evidence, the three that read a secret among them, so their role gets both secrets: on `docker` the task wrapper alone
keeps each task to its own. The evidence machines (`px_large_queue`) get a role of their own without either secret:
the evidence task reads none, and it is the task that parses every filing (a crafted filing that took over its
container would find no secret there). The shared Airflow workers keep only the intake policy (when `aws_default` has
no credentials of its own):

```sh
PX_WORKER_ROLE=<the instance role of the px workers: queue px_queue>
aws iam put-role-policy --role-name $PX_WORKER_ROLE --policy-name px-task --policy-document file://$OUT/task-role-policy.json
aws iam put-role-policy --role-name $PX_WORKER_ROLE --policy-name px-secret-sec --policy-document file://$OUT/task-secret-sec-policy.json
aws iam put-role-policy --role-name $PX_WORKER_ROLE --policy-name px-secret-llm --policy-document file://$OUT/task-secret-llm-policy.json
aws iam attach-role-policy --role-name $PX_WORKER_ROLE --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly
PX_LARGE_ROLE=<the instance role of the evidence machines: queue px_large_queue, not the px workers' role>
aws iam put-role-policy --role-name $PX_LARGE_ROLE --policy-name px-task --policy-document file://$OUT/task-role-policy.json
aws iam attach-role-policy --role-name $PX_LARGE_ROLE --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly
WORKER_ROLE=<the instance role of the shared Airflow workers>
aws iam put-role-policy --role-name $WORKER_ROLE --policy-name px-intake --policy-document file://$OUT/intake-role-policy.json
```

Backend `ecs`: three task roles, one per secret need, and a task execution role, all assumable by ECS tasks of the
account. The task definition names `px-task`; `company_evidence` runs task 2 as `px-task-sec` and tasks 3 and 6 as
`px-task-llm` (Variables `px_ecs_task_role_sec` and `px_ecs_task_role_llm`, section 8):

```sh
fill deploy/iam/task-role-trust-ecs.json > $OUT/task-role-trust-ecs.json
for ROLE in px-task px-task-sec px-task-llm px-task-execution; do
  aws iam create-role --role-name $ROLE --assume-role-policy-document file://$OUT/task-role-trust-ecs.json
done
for ROLE in px-task px-task-sec px-task-llm; do
  aws iam put-role-policy --role-name $ROLE --policy-name px-task --policy-document file://$OUT/task-role-policy.json
done
aws iam put-role-policy --role-name px-task-sec --policy-name px-secret-sec --policy-document file://$OUT/task-secret-sec-policy.json
aws iam put-role-policy --role-name px-task-llm --policy-name px-secret-llm --policy-document file://$OUT/task-secret-llm-policy.json
aws iam attach-role-policy --role-name px-task-execution \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
```

The intake policy then goes on the identity of `aws_default`, with the ECS permissions in the table.

## 6. Image

Build from the repository root. BuildKit reads `deploy/image/Dockerfile.dockerignore`, which lets in only the code the
image needs; the build fails if a `.env` file reaches the image. Build from a clean checkout, so that the tag (the
commit) describes what is in the image.

```sh
export IMAGE_TAG=$(git rev-parse --short=12 HEAD)
export IMAGE_URI=$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$ECR_REPO:$IMAGE_TAG
aws ecr create-repository --repository-name $ECR_REPO --image-tag-mutability IMMUTABLE      # once
aws ecr get-login-password | docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com
docker build --platform linux/amd64 -f deploy/image/Dockerfile -t $IMAGE_URI .
docker push $IMAGE_URI
```

`--platform` must match the CPU of the machines that run the tasks (`linux/arm64` for Graviton). The image runs
`python -m px_runner` as a non-root user; a task passes only `<task> --company <co> --run-id <run_id>`.

## 7. Where tasks run

Each task needs local disk for what it stages: tasks 2, 3, 5 and 7 copy all of the company's filings (21 GB for
Databricks, about 330 MB for Lungpacer), plus the work files.

**Backend `docker`** (Variable `px_task_backend` = `docker`). An Airflow worker starts the image with
`DockerOperator` through `px_docker_url`. The containers get AWS credentials from the worker machine's instance role
(instance metadata service, IMDS). So on a machine that runs px tasks, every container, and every Airflow task of
another DAG, can use the task policies, and on a `px_queue` worker read both secrets; only the task wrapper keeps each
task to its own secret. Backend `ecs` keeps them apart in IAM. On `docker`:

- Run the px tasks on workers of their own. Set the Variable `px_queue` (e.g. `px`) and start
  `airflow celery worker --queues px` on machines that run nothing else; every task but evidence goes to that queue.
  Without `px_queue` the tasks run on Airflow's default queue, on the shared workers, whose role would then need the
  task policies: acceptable only for a test deployment.
- The evidence task goes to the Celery queue `px_large_queue` (`evidence_large`). Start a worker for it on a machine
  sized for one evidence scan: `airflow celery worker --queues evidence_large --concurrency 1`. A queue only says
  where a task runs, so the DAG also lets only `px_large_slots` (1) evidence tasks run at once across all runs; with
  more large workers, raise it to their number. These machines get a role of their own, without the secret policies
  (section 5).
- Every px worker needs Docker, access to its socket for the Airflow user, and room in Docker's storage for the tasks
  it runs at once. It must be able to pull `$IMAGE_URI`: run `docker pull $IMAGE_URI` on each one after
  `docker login` (section 6), or install the Amazon ECR credential helper for the Airflow user.
- Require IMDSv2 on every worker (with IMDSv1 a container reaches IMDS whatever the hop limit). A container on
  Docker's default bridge network needs a hop limit of 2; set it on the px workers and evidence machines only:
  `aws ec2 modify-instance-metadata-options --instance-id <id> --http-tokens required --http-put-response-hop-limit 2`.
  Keep `--http-put-response-hop-limit 1` on the shared workers, so that containers there get no AWS credentials.
- When a worker dies while its task runs (restart, deploy), Docker keeps the task's container running. The task's
  next try stops and removes it before it starts its own container, when that try runs on the same machine
  (section 14). A try that Airflow stops from outside (the run's `px_run_timeout_hours`, the task marked failed) is
  killed before it removes its container, which stays exited, with the files it staged, until a task of the
  company's next run on that machine removes it.

**Backend `ecs`** (Variable `px_task_backend` = `ecs`). `EcsRunTaskOperator` runs the task definition on Fargate
with a command override. Register one task definition with:

- Fargate compatibility (`requiresCompatibilities` `FARGATE`, network mode `awsvpc`);
- one container named `px` (Variable `px_ecs_container`), image `$IMAGE_URI`;
- `taskRoleArn` = `px-task`, which reads no secret, and `executionRoleArn` = `px-task-execution` (section 5); task 2
  runs as `px-task-sec` and tasks 3 and 6 as `px-task-llm` (Variables `px_ecs_task_role_sec`, `px_ecs_task_role_llm`);
- log driver `awslogs` with group `/ecs/px-pipeline` (Variable `px_ecs_log_group`) and stream prefix `ecs`, so the
  Airflow task log shows the container output;
- CPU and memory for the light tasks, and `ephemeralStorage` large enough for the company's filings and work files
  (Fargate: 21 to 200 GiB). The evidence task runs with the overrides in `px_ecs_large_overrides` (default 8 vCPU,
  60 GiB memory, 200 GiB storage);
- private subnets with a NAT gateway (tasks 2, 3 and 6 call SEC and OpenRouter), and a security group with outbound
  HTTPS only (Variable `px_ecs_security_groups`, required: without one, ECS would use the VPC's default group).

On `ecs` the image comes from the task definition, not from `px_image`: for a new image, register a new revision; with
the family name in `px_ecs_task_definition`, runs use the latest revision.

## 8. DAGs, Variables and pool

Copy `deploy/dags` without its tests into a folder of its own in the Airflow DAG bundle: the DAG files
(`company_evidence.py`, `company_intake.py`) import `px_settings.py` and `px_intake.py` from their own folder, and its
`.airflowignore` keeps those two from being parsed as DAGs without replacing an `.airflowignore` the bundle already
has. Then load the Variables and the pool and unpause both DAGs:

```sh
rsync -av --exclude tests --exclude __pycache__ deploy/dags/ <dags folder>/px/
airflow dags list-import-errors
export PX_QUEUE=px                                                          # backend docker: the px workers' queue
export ECS_CLUSTER= ECS_TASK_DEFINITION= ECS_SUBNETS= ECS_SECURITY_GROUPS=    # backend docker; set them for ecs
export ECS_TASK_ROLE_SEC= ECS_TASK_ROLE_LLM=                                 # for ecs: arn:aws:iam::$ACCOUNT_ID:role/px-task-sec, ...-llm
export ALERT_EMAILS=ops@example.org                                         # comma-separated; empty: no e-mail
fill deploy/iam/airflow-variables.example.json > $OUT/airflow-variables.json
airflow variables import $OUT/airflow-variables.json
airflow pools import deploy/iam/airflow-pools.example.json
airflow dags unpause company_evidence
airflow dags unpause company_intake
```

New DAGs start paused. `company_intake` plans nothing while `company_evidence` is paused, and a run triggered by
hand waits until it is unpaused.

| Variable | Default | Meaning |
|---|---|---|
| `px_bucket` | `company-reports-data` | the S3 bucket |
| `px_region` | `us-east-2` | AWS region |
| `px_image` | `px-pipeline:latest` (a local name) | the image URI in ECR with its tag (section 6), backend `docker` |
| `px_task_backend` | `docker` | `docker` or `ecs` (section 7) |
| `px_docker_url` | `unix://var/run/docker.sock` | Docker daemon of the workers |
| `px_large_queue` | `evidence_large` | Celery queue of the evidence task (`docker`) |
| `px_queue` | none: Airflow's default queue | Celery queue of the other tasks (`docker`): the px workers (section 7) |
| `px_ecs_cluster`, `px_ecs_task_definition` | none | cluster; task definition family or `family:revision` (`ecs`) |
| `px_ecs_subnets`, `px_ecs_security_groups` | none | ids, comma-separated (`ecs`, both required) |
| `px_ecs_task_role_sec`, `px_ecs_task_role_llm` | none | ARNs of the task roles `px-task-sec` (task 2) and `px-task-llm` (tasks 3 and 6) (`ecs`, required) |
| `px_alert_emails` | none | failure e-mail recipients, comma-separated |
| `px_refresh_days` | `7` | run a company again when its last successful run ended this many days ago (new filings) |
| `px_stale_hours` | `24` | a `queued` or `running` record older than this no longer blocks a new run |
| `px_sec_pool` | `sec_api` | pool of task 2 |
| `px_secret_sec`, `px_secret_llm` | `px/sec-user-agent`, `px/openrouter-api-key` | the secret ids (section 4) |

`px_settings.py` also reads `px_ecs_container`, `px_ecs_log_group`, `px_ecs_large_overrides`, `px_task_env`
(extra, non-secret environment for every task, for example `{"PX_WORKERS": "8"}`), `px_large_slots` (section 7),
`px_task_timeout_hours` (12: each try of a task is stopped after this long, then retried) and `px_run_timeout_hours`
(72: a `company_evidence` run still not done after this long is failed and e-mailed; it counts the time spent waiting
for a pool slot, so allow for the queue of SEC downloads). Their defaults fit section 7. A Variable that Airflow cannot
read (an API or database error) fails the parse or the task, which is tried again; its default is never used for it.

The pool `sec_api` has one slot, so one SEC download runs at a time across all runs (the client paces itself at 5
requests per second; SEC allows 10). All SEC calls of this Airflow should use one pool: if the existing SEC ingestion
DAG has one, set `px_sec_pool` to it instead of creating `sec_api`.

## 9. Input file

One file per company at `s3://$BUCKET/input/<co>/company.json`. `<co>` is the company's folder name, used in every
S3 key and as the pipeline's config name; use lowercase letters, digits and `_`. The intake skips, with a warning in
its log, a folder name the tasks cannot take: one with a character other than letters, digits, `_` and `-` (such as
`.`), one that starts with `_` or `-`, or one longer than 100 characters. The file is the pipeline's config
(`pipeline/config/<co>.json`) plus `run_indirect`.

| Field | Needed | Used for |
|---|---|---|
| `name` | yes | short name: SEC full-text search phrase (task 2), a name for the finder (task 1); the workbook name when there is no `legal_name` |
| `company.legal_name` | recommended | the finder's canonical name, and `<Name>` in the evidence facts and the workbook: the file is `<legal_name up to the first comma, spaces as _>_findings.xlsx` |
| `company.aliases` | recommended | more SEC search phrases and finder names; no `\|` in a name |
| `company.description` | recommended | one or two sentences on what the company is and where, any former name: the model in task 3 reads it with each match to set aside look-alike organisations; without it, task 1 warns and the check judges from the name alone |
| `company.name_regex` | yes | what counts as a mention in the evidence and indirect steps, and the issuer filter of N-PORT; keep it RE2-compatible (no lookarounds or backreferences) for the N-PORT database |
| `company.cik` | yes | the company's own Form D notices (task 4 fails without it), and the finder's company id |
| `company.employee_company` | for the DOL sheet | the label of the company's rows in the DOL files |
| `k401_plan` | for the DOL sheet | the plan number of the 401(k) plan, e.g. `"001"` |
| `split_date`, `split_ratio` | if the stock was split | N-PORT prices per share before the date are divided by the ratio |
| `run_indirect` | no, default `true` | `false` skips task 6: no indirect-mention model cost, and the workbook has no indirect mentions. Task 3's look-alike check still calls the model (and needs the key) |

Other keys of the pipeline configs (`as_of`, `fy_end_month`, `health_plan`, `company.match`, `company.spv_key`) are
not read by tasks 1–7 and may stay. Example: `pipeline/config/lungpacer.json` with `legal_name` and `description`
added from the company's Form D notices (issuer name, address, place of incorporation, industry group):

```json
{
 "as_of": "2026-11-02",
 "fy_end_month": 12,
 "health_plan": "501",
 "k401_plan": "001",
 "name": "Lungpacer",
 "company": {
  "legal_name": "Lungpacer Medical, Inc.",
  "description": "Health care company (Form D industry group: Other Health Care) based in Exton, Pennsylvania and incorporated in British Columbia, Canada; earlier based in Burnaby, British Columbia. AeroPace is one of its names; \"Aeropace\" as a misspelling of \"aerospace\" in other filings is not this company.",
  "cik": "0001708893",
  "match": "LUNGPACER",
  "name_regex": "lungpacer",
  "aliases": [
   "Lungpacer",
   "AeroPace"
  ],
  "employee_company": "Lungpacer Medical",
  "spv_key": "lungpacer"
 },
 "run_indirect": true
}
```

## 10. S3 layout

```
s3://company-reports-data/
  input/<co>/company.json                                the input file (analyst)
  raw/<co>/sec/manifest.csv                              filings as downloaded, kept across runs
  raw/<co>/sec/files/<form>/<accession>_<file>
  intermediate/<co>/<run_id>/mentions/                   task 1 companies file; task 3 <co>_mentions.csv, <co>_dropped.csv
  intermediate/<co>/<run_id>/records/                    task 4: inputs/, formd_offerings.csv, nport/
  intermediate/<co>/<run_id>/evidence/                   task 5
  intermediate/<co>/<run_id>/prepared/                   task 5: companies/, pdfs/ (input of task 6)
  intermediate/<co>/<run_id>/indirect/                   task 6: model answers, *_indirect_run.json, run.log,
                                                         *_indirect_input.json, model_cost.json (the runner's)
  intermediate/<co>/<run_id>/publish/indirect_located.csv  task 7
  reports/<co>/<run_id>/<file>.xlsx                      the workbook of each run
  reports/<co>/latest/<file>.xlsx                        the workbook of the latest successful publish
  runs/<co>/<run_id>.json                                run record
  reference/formd/  reference/dol/  reference/nport/     shared, read-only (section 3)
```

`raw/` is never deleted: a run downloads only filings not yet there and uploads only new or changed files.
`intermediate/` is kept per run, for audit and for re-running a task. A task that runs again replaces its own folders
there with what it writes this time, so a later task never reads a filing or table that the earlier attempt left.
Task 6 keeps a filing's earlier answers only when they were made from the same prepared files (the filing's
`*_indirect_input.json`). Each key and its local path are listed in
[INTERFACES.md](INTERFACES.md) (S3 keys). `run_id` is `<UTC time YYYYMMDDTHHMMSSZ>-<first 8 characters of the input
file's ETag>`, so run ids sort by time.

## 11. Run record

`runs/<co>/<run_id>.json`, one per run:

```
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

- `company_intake` writes it as `queued`; the first task sets `running` (and creates the record if it is missing,
  with the input file's ETag); each task writes its own entry under `tasks`; `finalize` sets `succeeded` (tasks 1–7
  all succeeded or skipped) or `failed`, and `ended_at`. Times are UTC.
- `outputs` holds each task's counts, for example `records` → `not_built` (optional sources that were missing: N-PORT
  and the DOL welfare file; a Form D that cannot be built fails the task), `indirect` → `failed_calls` and
  `redone_filings`, and `publish` → the row count of each sheet. `error` holds the last lines of a failed script, with
  anything that looks like a key removed.
- `model_cost_usd.indirect` is the model cost of task 6, including answers a retry replaced, over all its attempts
  in the run (`intermediate/<co>/<run_id>/indirect/model_cost.json` lists each run file's cost). `finder` stays
  `null`: company-finder does not report its cost; OpenRouter's usage page for the key shows it.

```sh
aws s3 ls s3://$BUCKET/runs/lungpacer/                                   # one file per run, oldest first
aws s3 cp s3://$BUCKET/runs/lungpacer/<run_id>.json - | python3 -m json.tool
aws s3 cp --recursive s3://$BUCKET/reports/lungpacer/latest/ .           # the newest workbook
```

## 12. Start and re-run a company

**Start.** Upload the input file. Within 15 minutes `company_intake` writes a `queued` record and triggers
`company_evidence` with `{"company": "<co>", "run_id": "<run_id>"}`. To start it at once, trigger `company_intake` in
the Airflow UI (or `airflow dags trigger company_intake`).

```sh
aws s3 cp lungpacer.json s3://$BUCKET/input/lungpacer/company.json
```

Follow the run in the Airflow grid view of `company_evidence` (one run per company and run id; task logs show the
pipeline's output) and in the run record. A failed task sends an e-mail to `px_alert_emails` with the record's path;
`finalize` then fails the DAG run without a second e-mail. `finalize` retries like the other tasks when it did not
work itself (a Docker or ECS error, S3), so an e-mail from `finalize` means that it still did not work after its
retries (clear it once the cause is fixed). A run still not done after `px_run_timeout_hours` and a failed
`company_intake` run send an e-mail of their own. The workbook ends up in `reports/<co>/<run_id>/` and
`reports/<co>/latest/`.

**When the intake starts a run.** For each `input/<co>/company.json`: when the company has no run record; when the
file's ETag differs from the latest record's `input_etag` (the file was changed); or when the latest successful run
ended more than `px_refresh_days` ago. Never while the company's latest record is `queued` or `running` and younger
than `px_stale_hours`. After a failed run, the intake starts the company again only when its input changes or
`px_refresh_days` after that run (`deploy/dags/px_intake.py`); re-run it yourself once the cause is fixed. A run
whose tasks 1–7 all finished but whose `finalize` failed counts as succeeded (clear `finalize` to record it). The
intake writes a run's `queued` record only when it does not exist, so clearing a `company_intake` task never resets
the record of a run that has started.

**Re-run failed tasks of the same run.** Each task retries twice, 5 minutes apart. After that, fix the cause and, in
the Airflow grid view of that run, clear the failed task with its downstream tasks. They run again with the same
`run_id`, read what the earlier tasks wrote to `intermediate/<co>/<run_id>/`, and `finalize` sets the overall status
again. Task 6 keeps the filings an earlier attempt finished, also when that attempt was stopped, so a retry pays only
for the rest; a filing whose model calls failed runs again, and so does one whose prepared files changed (finder or
evidence ran again and kept other mentions). Each try of a task is stopped after `px_task_timeout_hours`.

**Start a new run without changing the input.** Write the run's `queued` record, as the intake does, then trigger
`company_evidence` with that run id as both the Airflow run id and the conf's `run_id`. The record keeps the intake
from starting another run of the company while this one waits for a free slot (`max_active_runs`); every task fails
at once when the two run ids differ, because the intake finds a run in Airflow by the record's id.

```sh
CO=lungpacer
ETAG=$(aws s3api head-object --bucket $BUCKET --key input/$CO/company.json --query ETag --output text | tr -d '"')
NOW=$(date -u +%Y%m%dT%H%M%SZ)
RUN_ID=$NOW-${ETAG:0:8}
CREATED=$(echo "$NOW" | sed -E 's/(....)(..)(..)T(..)(..)(..)Z/\1-\2-\3T\4:\5:\6Z/')
printf '{"company": "%s", "run_id": "%s", "status": "queued", "input_etag": "%s", "created_at": "%s", "tasks": {}}\n' \
    "$CO" "$RUN_ID" "$ETAG" "$CREATED" | aws s3 cp - s3://$BUCKET/runs/$CO/$RUN_ID.json --content-type application/json
airflow dags trigger company_evidence --run-id "$RUN_ID" --conf "{\"company\": \"$CO\", \"run_id\": \"$RUN_ID\"}"
```

In the UI: Trigger, with the same run id and conf (after writing the record). `airflow dags test` cannot set the run
id, so its runs stop at the first task. Do not start a run of a company while another run of it is `queued` or
`running`: both would write to `raw/<co>/`.

**Change or stop a company.** Upload a changed input file when no run of the company is active; the intake starts a
new run. Each task reads the input file when it starts, so a change made during a run reaches the tasks that start
after it (the task log warns). To stop new runs of a company, delete its `input/<co>/company.json`; its data stays.

## 13. Local end-to-end check

See [deploy/e2e/README.md](e2e/README.md).

## 14. Known limits

- **Only the main dump folder.** Task 2 downloads into `<co>` only; the `<co>_ncsr` folder (fund reports found with
  extra `--term` phrases) and `<co>_delta` are not built or read here, while `pipeline/run_company.py` reads all three
  when their manifests exist. For Databricks the archived manifests list 1,050 filings in `databricks`, 959 in
  `databricks_ncsr` and 53 in `databricks_delta`, so a run here sees about half of them, and its workbook will not
  match the current Databricks workbook (the Phase 1 acceptance test) until those folders are supported.
- **DOL welfare file not produced today.** `reference/dol/data/welfare_plans_by_year.csv` does not exist yet.
  `records.py` writes `dol_form5500.csv` and `plan_participants.csv`, then reports `MISSING DOL Form 5500`
  (FileNotFoundError on that file), so `welfare_plans.csv` and `dol_5500_sch_h.csv` are not written. The DOL 401(k)
  sheet reads `plan_participants.csv` and is filled; task 4 succeeds and lists the source under `not_built`.
- **N-PORT needs a per-company file or the database on the worker.** Task 4 uses `reference/nport/<co>/nport_holdings.csv`
  (section 3), or, when `PX_NPORT_DB` is set in the task's environment (`px_task_env`), runs `nport.py export-db` and
  `build` on that file. The database is 15 GB and must be visible inside the task's container. On `ecs` that can be an
  EFS volume declared in the task definition; on `docker` the DAG mounts no host folder into the container, so the
  per-company file is the only way. Without either, the N-PORT sheet is empty and holdings have no N-PORT price marks
  to be checked against.
- **Model text varies between runs.** Tasks 3 and 6 call models whose answers are not repeatable word for word: a
  re-run words the reasons differently. Compare runs on values and decisions; each run's model outputs stay in
  `intermediate/<co>/<run_id>/` (`mentions/`, `indirect/`).
- **Finder `--out` naming.** company-finder names its files after `--out`: `X.csv` (one row per company and filing),
  `X_mentions.csv` (one row per mention), `X_dropped.csv`, `X_summary.csv`. `prepare_filings.py` reads
  `<co>_mentions.csv` as one row per mention, which holds only with `--out <co>.csv`. With `--out <co>_mentions.csv`
  (the repository README's step 3), `<co>_mentions.csv` is the per-filing summary instead. Task 3 always uses
  `--out <co>.csv`; use the same name when running the finder by hand.
- **Form D.** The company's CIK must be in `reference/formd/formd.duckdb` (section 3); otherwise task 4 fails.
- **Access is not limited per company.** Every task role (and on `docker` the px workers' role) can read and write
  every company's `raw/`, `intermediate/`, `reports/` and `runs/`, so a task that is made to misbehave, for example
  by a crafted filing, could change another company's files. Limiting a task to its own company needs a design
  change (the task wrapper taking a role with a session policy for its company's prefixes, or a role per company) and
  is not built; see [iam/README.md](iam/README.md).
- **Backend `docker`: one AWS identity per worker machine.** Every task on a `px_queue` worker, and every container
  there, can read both secrets; only the task wrapper keeps each task to its own (section 7). The evidence machines'
  role has neither (section 5). Backend `ecs` keeps them apart in IAM.
- **Backend `docker`: a container left behind on another worker.** When a worker dies while its task runs, the next
  try stops the old container only when it runs on the same machine (with one large worker, an evidence retry
  always does). A try on another px worker runs beside the old container until that one ends, both writing the run's
  files and record. After a px worker died, run `docker ps --filter name=px-` on it and stop the containers of runs
  that Airflow has moved on (`docker stop`, which lets the task record the stop). Backend `ecs` finds the task it
  started instead of starting a second one.
- **Backend `docker`: exited containers of runs stopped from outside.** When Airflow stops a running try from outside
  (`px_run_timeout_hours`, the task marked failed or skipped), it kills the task 5 seconds later, while the container
  still gets up to 60 seconds to record the stop, so the container is not removed. It stays exited, with the files it
  staged (21 GB for a Databricks evidence task), until a task of the company's next run on that machine removes it.
  To free the space sooner: `docker ps -a --filter name=px- --filter status=exited`, then `docker rm` those.
