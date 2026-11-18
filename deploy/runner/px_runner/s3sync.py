"""S3 <-> local disk for one task: the key table of deploy/INTERFACES.md (Layout) and the transfers (Bucket).

Nothing in raw/, input/, reference/ or runs/ is deleted. Two kinds of delete exist: publish replaces the workbook under
reports/<co>/latest/, and a task's output folders under intermediate/<co>/<run_id>/ hold exactly what its latest
attempt wrote (sync_dir, sync_file), so a re-run task never leaves an earlier attempt's files for the next task. A
delete that S3 refuses fails the task (DeleteFailed), so no earlier file is left behind without an error.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from botocore.exceptions import ClientError

SKIP = (".part", ".tmp")            # half-written files an interrupted script can leave behind; never uploaded


class Loc(NamedTuple):
    """An S3 key (a folder when it ends with '/') and the local path it is staged to."""
    key: str
    path: Path


class Layout:
    """Every S3 key a task of one run reads or writes and its local path. R is PX_LOCAL_ROOT: the pipeline sees R/data
    as PX_DATA, R/data/dumps as PX_DUMPS, R/finder as PX_FINDER_ROOT and R/work as PX_WORK."""

    def __init__(self, root: Path, repo: Path, co: str, run_id: str):
        R, i = Path(root), f"intermediate/{co}/{run_id}"
        self.root, self.data, self.dumps = R, R / "data", R / "data" / "dumps"
        self.finder, self.work, self.reports = R / "finder", R / "work", R / "reports"
        out, emp = self.work / "out" / co, self.dumps / "employee_docs"
        # folders: <prefix><path> <-> <folder>/<path>
        self.raw = Loc(f"raw/{co}/sec/files/", self.dumps / co)
        self.mentions = Loc(f"{i}/mentions/", self.finder)
        self.records_inputs = Loc(f"{i}/records/inputs/", self.work / "inputs" / co)
        self.records_nport = Loc(f"{i}/records/nport/", self.dumps / f"{co}_nport")
        self.evidence = Loc(f"{i}/evidence/", out / "evidence")
        self.prepared = Loc(f"{i}/prepared/", self.work / "prepared")
        self.indirect = Loc(f"{i}/indirect/", out / "indirect")
        self.report_run = Loc(f"reports/{co}/{run_id}/", self.reports)
        self.report_latest = Loc(f"reports/{co}/latest/", self.reports)
        # single files
        self.config = Loc(f"input/{co}/company.json", Path(repo) / "pipeline" / "config" / f"{co}.json")
        self.manifest = Loc(f"raw/{co}/sec/manifest.csv", self.dumps / f"{co}_manifest.csv")
        self.formd_offerings = Loc(f"{i}/records/formd_offerings.csv", out / "formd_offerings.csv")
        self.located = Loc(f"{i}/publish/indirect_located.csv", out / "indirect_located.csv")
        self.record = f"runs/{co}/{run_id}.json"
        # shared reference data (read only)
        self.formd_db = Loc("reference/formd/formd.duckdb", self.data / "input" / "formd" / "formd.duckdb")
        self.nport_ref = Loc(f"reference/nport/{co}/nport_holdings.csv", self.dumps / f"{co}_nport" / "nport_holdings.csv")
        self.employee_dir = emp / "raw"
        self.dol_rows = [Loc(f"reference/dol/raw/{n}", emp / "raw" / n)
                         for n in ("form5500_rows.csv", "form5500sf_rows.csv", "schedule_h_rows.csv")]
        self.dol_welfare = Loc("reference/dol/data/welfare_plans_by_year.csv", emp / "data" / "welfare_plans_by_year.csv")


def _missing(e: ClientError) -> bool:
    return e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound")


class DeleteFailed(RuntimeError):
    """S3 did not delete some of the keys of a multi-object delete (it answers HTTP 200 and lists them under Errors)."""


class Bucket:
    """Transfers between one S3 bucket and local disk; folders go through a small thread pool."""

    def __init__(self, client, name: str, threads: int = 16):
        self.s3, self.name, self.threads = client, name, threads

    def sizes(self, prefix: str) -> dict[str, int]:
        """Key -> size of every object under prefix."""
        out: dict[str, int] = {}
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.name, Prefix=prefix):
            out.update((o["Key"], o["Size"]) for o in page.get("Contents", []))
        return out

    def read(self, key: str) -> tuple[bytes, str] | None:
        """(content, ETag without quotes), or None when the object does not exist."""
        try:
            r = self.s3.get_object(Bucket=self.name, Key=key)
        except ClientError as e:
            if _missing(e):
                return None
            raise
        return r["Body"].read(), r["ETag"].strip('"')

    def etag(self, key: str) -> str | None:
        try:
            return self.s3.head_object(Bucket=self.name, Key=key)["ETag"].strip('"')
        except ClientError as e:
            if _missing(e):
                return None
            raise

    def write(self, key: str, data: bytes, content_type: str = "application/json") -> None:
        self.s3.put_object(Bucket=self.name, Key=key, Body=data, ContentType=content_type)

    def download(self, key: str, path: Path) -> bool:
        """One object to path; False when it does not exist."""
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.s3.download_file(self.name, key, str(path))
        except ClientError as e:
            if _missing(e):
                return False
            raise
        return True

    def download_dir(self, prefix: str, dest: Path) -> int:
        """Every object under prefix to dest/<rest of the key>. Returns the number of files."""
        jobs = []
        for key in self.sizes(prefix):
            rel = PurePosixPath(key[len(prefix):])
            if key.endswith("/") or not rel.parts:                     # a folder marker
                continue
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"unsafe S3 key under {prefix}: {key}")
            jobs.append((key, dest.joinpath(*rel.parts)))
        self._each(lambda job: self.download(*job), jobs)
        return len(jobs)

    def upload(self, path: Path, key: str) -> None:
        self.s3.upload_file(str(path), self.name, key)

    def upload_dir(self, src: Path, prefix: str, unchanged: dict[str, int] | None = None,
                   last: tuple[str, ...] = ()) -> list[tuple[str, Path]]:
        """Every file under src to <prefix><relative path>, except half-written ones and, given unchanged
        (key -> size), files already in S3 with the same size. Files whose names end with a suffix in last go up after
        all the others, the files of each suffix after those of the one before it, so whoever finds such a file in S3
        finds the files uploaded before it there too. Returns the (key, path) pairs uploaded."""
        passes: list[list[tuple[str, Path]]] = [[] for _ in range(len(last) + 1)]
        for folder, _, names in os.walk(src):
            for name in sorted(names):
                path = Path(folder) / name
                key = prefix + path.relative_to(src).as_posix()
                if name.endswith(SKIP) or (unchanged is not None and unchanged.get(key) == path.stat().st_size):
                    continue
                passes[next((i for i, suffix in enumerate(last, 1) if name.endswith(suffix)), 0)].append((key, path))
        for jobs in passes:
            self._each(lambda job: self.upload(job[1], job[0]), jobs)
        return sorted(job for jobs in passes for job in jobs)

    def sync_dir(self, src: Path, prefix: str, last: tuple[str, ...] = ()) -> list[tuple[str, Path]]:
        """upload_dir (last: see there), then delete every other object under prefix: the prefix then holds what src
        holds, and an earlier attempt's files that this one did not write are gone. Only for a task's own
        intermediate/ folders."""
        before = self.sizes(prefix)
        up = self.upload_dir(src, prefix, last=last)
        self.delete(sorted(set(before) - {k for k, _ in up}))
        return up

    def sync_file(self, path: Path, key: str) -> list[tuple[str, Path]]:
        """Upload path to key; when this attempt wrote no such file, delete the one an earlier attempt left there."""
        if path.is_file():
            self.upload(path, key)
            return [(key, path)]
        if key in self.sizes(key):
            self.delete([key])
        return []

    def delete(self, keys: list[str]) -> None:
        """Delete keys, 1,000 per request. S3 checks a multi-object delete key by key and answers HTTP 200 also when it
        deleted none of them, listing those under Errors (AccessDenied without s3:DeleteObject on the key, InternalError,
        SlowDown); boto3 raises nothing for that, so this raises DeleteFailed."""
        errors: list[dict] = []
        for i in range(0, len(keys), 1000):
            resp = self.s3.delete_objects(Bucket=self.name, Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]]})
            errors += (resp or {}).get("Errors") or []
        if errors:
            shown = "; ".join(f"{e.get('Key')} ({e.get('Code')}: {e.get('Message')})" for e in errors[:3])
            denied = any(e.get("Code") == "AccessDenied" for e in errors)
            raise DeleteFailed(f"S3 did not delete {len(errors)} of {len(keys)} object(s) in s3://{self.name}/, for "
                               f"example {shown}" + ("; the task role needs s3:DeleteObject on them "
                                                     "(deploy/iam/README.md, Deletes)" if denied else ""))

    def _each(self, fn, jobs: list) -> None:
        with ThreadPoolExecutor(self.threads) as pool:
            list(pool.map(fn, jobs))
