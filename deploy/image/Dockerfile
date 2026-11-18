# syntax=docker/dockerfile:1

# Pipeline image for the company evidence pipeline on AWS (deploy/ARCHITECTURE.html, deploy/INTERFACES.md).
# Every Airflow task runs this image with its own arguments; the entrypoint is `python -m px_runner`, so a task
# passes only `<task> --company <co> --run-id <run_id>`.
#
# Build from the repository root with BuildKit (Docker's default builder), which reads Dockerfile.dockerignore
# next to this file:
#   docker build -f deploy/image/Dockerfile -t px-pipeline:dev .
# Add --platform linux/amd64 when the build machine is arm64 (Apple silicon) and the image runs on x86_64 hosts.
#
# /opt/project-x holds pipeline/, tools/, company-finder/ (code and requirements.txt) and deploy/runner/, nothing
# else. Secrets never enter the image: tasks read them from Secrets Manager at run time. company-finder/.env is kept
# out three ways: the dockerignore lets in only the paths the image uses, the COPY lines name each path, and the
# build fails if a .env file reaches /opt/project-x.

ARG PYTHON_IMAGE=python:3.12-slim


# ---- runner: the task wrapper's source, deploy/runner/. A stage of its own so that a build can supply another
# copy of it: docker build --build-context runner=<folder> ...
FROM scratch AS runner
COPY deploy/runner/ /


# ---- the image
FROM ${PYTHON_IMAGE}

# Non-root user px (uid and gid 10001). It can write /tmp/px (PX_LOCAL_ROOT), its home (company-finder keeps its
# matcher cache in ~/.cache) and pipeline/config/ (the runner writes <co>.json there); the code stays root-owned.
RUN groupadd --gid 10001 px \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin px \
 && install -d -o px -g px /tmp/px

# The pipeline's dependencies (company-finder/requirements.txt) at the versions in constraints.txt. Each has a binary
# wheel for linux/amd64 and linux/arm64, so no compiler or other system package is installed; --only-binary makes a
# package without a wheel fail the build here instead of being compiled.
RUN --mount=type=bind,source=company-finder/requirements.txt,target=/tmp/requirements.txt \
    --mount=type=bind,source=deploy/image/constraints.txt,target=/tmp/constraints.txt \
    --mount=type=cache,target=/root/.cache/pip \
    pip install --root-user-action=ignore --only-binary=:all: -r /tmp/requirements.txt -c /tmp/constraints.txt

WORKDIR /opt/project-x

# The task wrapper (distribution px-runner, depends on boto3). The build's leftovers in its source folder go.
COPY --from=runner / deploy/runner/
RUN --mount=type=bind,source=deploy/image/constraints.txt,target=/tmp/constraints.txt \
    --mount=type=cache,target=/root/.cache/pip \
    pip install --root-user-action=ignore -c /tmp/constraints.txt /opt/project-x/deploy/runner \
 && rm -rf deploy/runner/build deploy/runner/*.egg-info deploy/runner/src/*.egg-info \
 && pip check

# The pipeline code, unchanged, path by path.
COPY pipeline/ pipeline/
COPY tools/ tools/
COPY company-finder/company_finder.py company-finder/requirements.txt company-finder/
COPY company-finder/company_finder/ company-finder/company_finder/
RUN chown -R px:px pipeline/config \
 && if find /opt/project-x -name '.env' -o -name '.env.*' | grep -q .; then \
        echo 'error: a .env file reached /opt/project-x (see deploy/image/Dockerfile.dockerignore)' >&2; exit 1; \
    fi

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PX_LOCAL_ROOT=/tmp/px \
    PX_REPO=/opt/project-x

# px, by number so that a non-root check can verify it
USER 10001:10001
ENTRYPOINT ["python", "-m", "px_runner"]
CMD ["--help"]
