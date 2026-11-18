"""The S3 key <-> local path table of deploy/INTERFACES.md, and the transfers."""
from pathlib import Path

import pytest
from conftest import BUCKET, keys, put

from px_runner import s3sync, tasks

R, REPO, RUN = Path("/R"), Path("/repo"), "20261117T101500Z-1a2b3c4d"
I = f"intermediate/acme/{RUN}"

# (Layout attribute, S3 key, local path): the rows of the contract's table, written out for <co> = acme
CONTRACT = [
    ("config", "input/acme/company.json", "/repo/pipeline/config/acme.json"),
    ("manifest", "raw/acme/sec/manifest.csv", "/R/data/dumps/acme_manifest.csv"),
    ("raw", "raw/acme/sec/files/", "/R/data/dumps/acme"),
    ("mentions", f"{I}/mentions/", "/R/finder"),
    ("records_inputs", f"{I}/records/inputs/", "/R/work/inputs/acme"),
    ("formd_offerings", f"{I}/records/formd_offerings.csv", "/R/work/out/acme/formd_offerings.csv"),
    ("records_nport", f"{I}/records/nport/", "/R/data/dumps/acme_nport"),
    ("evidence", f"{I}/evidence/", "/R/work/out/acme/evidence"),
    ("prepared", f"{I}/prepared/", "/R/work/prepared"),
    ("indirect", f"{I}/indirect/", "/R/work/out/acme/indirect"),
    ("located", f"{I}/publish/indirect_located.csv", "/R/work/out/acme/indirect_located.csv"),
    ("report_run", f"reports/acme/{RUN}/", "/R/reports"),
    ("report_latest", "reports/acme/latest/", "/R/reports"),
    ("formd_db", "reference/formd/formd.duckdb", "/R/data/input/formd/formd.duckdb"),
    ("nport_ref", "reference/nport/acme/nport_holdings.csv", "/R/data/dumps/acme_nport/nport_holdings.csv"),
    ("dol_welfare", "reference/dol/data/welfare_plans_by_year.csv",
     "/R/data/dumps/employee_docs/data/welfare_plans_by_year.csv"),
]


@pytest.mark.parametrize("attr,key,path", CONTRACT, ids=[c[0] for c in CONTRACT])
def test_layout_matches_the_contract_table(attr, key, path):
    assert getattr(s3sync.Layout(R, REPO, "acme", RUN), attr) == s3sync.Loc(key, Path(path))


def test_layout_dol_rows_record_and_pipeline_folders():
    lay = s3sync.Layout(R, REPO, "acme", RUN)
    assert lay.dol_rows == [s3sync.Loc(f"reference/dol/raw/{n}", Path("/R/data/dumps/employee_docs/raw") / n)
                            for n in ("form5500_rows.csv", "form5500sf_rows.csv", "schedule_h_rows.csv")]
    assert lay.employee_dir == Path("/R/data/dumps/employee_docs/raw")
    assert lay.record == f"runs/acme/{RUN}.json"
    env = tasks.pipeline_env({"PATH": "/bin"}, lay)
    assert {k: env[k] for k in ("PX_DATA", "PX_DUMPS", "PX_FINDER_ROOT", "PX_WORK")} == {
        "PX_DATA": "/R/data", "PX_DUMPS": "/R/data/dumps", "PX_FINDER_ROOT": "/R/finder", "PX_WORK": "/R/work"}


def test_download_dir_maps_keys_below_the_prefix_to_paths(aws, tmp_path):
    s3, _ = aws
    put(s3, f"{I}/mentions/input/acme_input.csv", "id\n")
    put(s3, f"{I}/mentions/out/acme/acme_mentions.csv", "company_id\n")
    put(s3, f"{I}/mentions/out/", "")                                   # a console "folder" marker
    put(s3, f"{I}/mentions_other/x.csv", "not under the prefix\n")
    n = s3sync.Bucket(s3, BUCKET).download_dir(f"{I}/mentions/", tmp_path / "finder")
    assert n == 2
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()) == [
        "finder/input/acme_input.csv", "finder/out/acme/acme_mentions.csv"]


def test_download_dir_refuses_a_key_that_leaves_the_folder(aws, tmp_path):
    s3, _ = aws
    put(s3, f"{I}/evidence/../../../escape.txt", "x")
    with pytest.raises(ValueError, match="unsafe S3 key"):
        s3sync.Bucket(s3, BUCKET).download_dir(f"{I}/evidence/", tmp_path / "evidence")
    assert not list(tmp_path.rglob("escape.txt"))


def test_upload_dir_skips_half_written_files_and_unchanged_sizes(aws, tmp_path):
    s3, _ = aws
    src = tmp_path / "acme"
    for rel, text in {"10-K/a.htm": "same", "10-K/b.htm": "grew!", "8-K/c.htm": "new", "8-K/d.htm.part": "half",
                      "e.tmp": "half"}.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(text)
    before = {"raw/acme/sec/files/10-K/a.htm": 4, "raw/acme/sec/files/10-K/b.htm": 4}
    up = s3sync.Bucket(s3, BUCKET).upload_dir(src, "raw/acme/sec/files/", unchanged=before)
    assert [k for k, _ in up] == ["raw/acme/sec/files/10-K/b.htm", "raw/acme/sec/files/8-K/c.htm"]
    assert keys(s3) == {"raw/acme/sec/files/10-K/b.htm", "raw/acme/sec/files/8-K/c.htm"}


def test_missing_objects_and_delete(aws, tmp_path):
    s3, _ = aws
    b = s3sync.Bucket(s3, BUCKET)
    assert b.read("nope.json") is None and b.etag("nope.json") is None
    assert b.download("nope.json", tmp_path / "nope.json") is False
    assert b.upload_dir(tmp_path / "no-such-folder", "x/") == []
    put(s3, "reports/acme/latest/Old_findings.xlsx", "x")
    data, etag = b.read("reports/acme/latest/Old_findings.xlsx")
    assert data == b"x" and etag and '"' not in etag and etag == b.etag("reports/acme/latest/Old_findings.xlsx")
    b.delete(["reports/acme/latest/Old_findings.xlsx"])
    assert keys(s3) == set()


def test_sync_dir_and_sync_file_leave_only_what_this_attempt_wrote(aws, tmp_path):
    s3, _ = aws
    b = s3sync.Bucket(s3, BUCKET)
    for key in (f"{I}/evidence/old.csv", f"{I}/evidence/sub/old.json", f"{I}/evidence/kept.csv",
                f"{I}/evidence_other/x.csv", f"{I}/publish/indirect_located.csv", f"{I}/publish/indirect_located.csv.1"):
        put(s3, key, "an earlier attempt")
    src = tmp_path / "evidence"
    (src / "sub").mkdir(parents=True)
    (src / "kept.csv").write_text("new")
    (src / "sub" / "new.json").write_text("{}")
    (src / "half.csv.part").write_text("half")                         # never uploaded
    up = b.sync_dir(src, f"{I}/evidence/")
    assert [k for k, _ in up] == [f"{I}/evidence/kept.csv", f"{I}/evidence/sub/new.json"]
    assert keys(s3, f"{I}/evidence") == {f"{I}/evidence/kept.csv", f"{I}/evidence/sub/new.json",
                                         f"{I}/evidence_other/x.csv"}  # another prefix is not touched
    assert b.sync_file(tmp_path / "indirect_located.csv", f"{I}/publish/indirect_located.csv") == []
    assert keys(s3, f"{I}/publish/") == {f"{I}/publish/indirect_located.csv.1"}
    (tmp_path / "indirect_located.csv").write_text("run\n")
    assert b.sync_file(tmp_path / "indirect_located.csv", f"{I}/publish/indirect_located.csv") == [
        (f"{I}/publish/indirect_located.csv", tmp_path / "indirect_located.csv")]
    assert b.sync_dir(tmp_path / "no-such-folder", f"{I}/evidence/") == [] and not keys(s3, f"{I}/evidence/")


class Recorder:
    """The S3 client, recording uploads and deletes in the order they are made."""

    def __init__(self, s3):
        self.s3, self.calls = s3, []

    def __getattr__(self, name):
        return getattr(self.s3, name)

    def upload_file(self, path, bucket, key):
        self.calls.append(("upload", key))
        return self.s3.upload_file(path, bucket, key)

    def delete_objects(self, Bucket, Delete):
        self.calls += [("delete", o["Key"]) for o in Delete["Objects"]]
        return self.s3.delete_objects(Bucket=Bucket, Delete=Delete)


def test_sync_dir_uploads_the_last_suffixes_after_the_rest_and_deletes_after_every_upload(aws, tmp_path):
    """indirect/: a run file in S3 is never newer than the CSV files beside it, nor an input file than its run file."""
    s3, _ = aws
    put(s3, f"{I}/indirect/old_indirect_run.json", "{}")
    src = tmp_path / "indirect"
    src.mkdir()
    for stem in ("a", "b", "c"):
        for suffix in tasks.INDIRECT_FILES:
            (src / f"{stem}{suffix}").write_text("x")
    (src / "run.log").write_text("x")
    rec = Recorder(s3)
    up = s3sync.Bucket(rec, BUCKET).sync_dir(src, f"{I}/indirect/", last=(tasks.RUN_JSON, tasks.INPUT_JSON))
    phase = ["upload", tasks.RUN_JSON, tasks.INPUT_JSON, "delete"]
    seen = [op if op == "delete" else next((s for s in phase[1:3] if key.endswith(s)), "upload") for op, key in rec.calls]
    assert seen == sorted(seen, key=phase.index) and [seen.count(p) for p in phase] == [7, 3, 3, 1]
    assert len(up) == 13 and keys(s3, f"{I}/indirect/") == {k for k, _ in up}


class Refusing:
    """The S3 client, except that DeleteObjects answers as S3 does for keys it does not delete (AccessDenied for a key
    the role may not delete, InternalError): HTTP 200 with those keys under Errors, which boto3 does not raise."""

    def __init__(self, s3, refuse):
        self.s3, self.refuse = s3, refuse

    def __getattr__(self, name):
        return getattr(self.s3, name)

    def delete_objects(self, Bucket, Delete):
        keys_ = [o["Key"] for o in Delete["Objects"]]
        if allowed := [{"Key": k} for k in keys_ if k not in self.refuse]:
            self.s3.delete_objects(Bucket=Bucket, Delete={"Objects": allowed})
        return {"Errors": [{"Key": k, "Code": self.refuse[k], "Message": "refused"} for k in keys_ if k in self.refuse]}


def test_delete_fails_when_s3_leaves_keys_and_says_which(aws):
    s3, _ = aws
    gone, kept = f"{I}/prepared/pdfs/a.htm", f"{I}/prepared/pdfs/b.htm"
    for key in (gone, kept, "reports/acme/latest/Old_findings.xlsx"):
        put(s3, key, "x")
    with pytest.raises(s3sync.DeleteFailed, match=rf"did not delete 1 of 2 object\(s\).*{kept} \(AccessDenied.*s3:DeleteObject"):
        s3sync.Bucket(Refusing(s3, {kept: "AccessDenied"}), BUCKET).delete([gone, kept])
    assert gone not in keys(s3) and kept in keys(s3)
    with pytest.raises(s3sync.DeleteFailed, match=r"InternalError") as raised:   # not a permission: no IAM hint
        s3sync.Bucket(Refusing(s3, {kept: "InternalError"}), BUCKET).delete([kept])
    assert "s3:DeleteObject" not in str(raised.value)
    b = s3sync.Bucket(Refusing(s3, {"reports/acme/latest/Old_findings.xlsx": "AccessDenied"}), BUCKET)
    with pytest.raises(s3sync.DeleteFailed):                            # sync_file and sync_dir fail the same way
        b.sync_file(Path("/no/such/file.xlsx"), "reports/acme/latest/Old_findings.xlsx")
