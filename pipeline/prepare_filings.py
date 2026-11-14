"""Line up one dump folder's filings with their company-finder mentions, for the indirect step.

For a dump folder such as `databricks` (or `databricks_ncsr`):
  * reads dumps/<folder>_manifest.csv (form, date, filer of every downloaded filing),
  * reads the finder's <folder>_mentions.csv (kept mentions after its false-positive check; one file for all the
    folder's filings, the `document` column says which filing each mention is in),
  * for every filing that has mentions, copies (or links) the filing to work/prepared/pdfs/ and writes
      work/prepared/companies/<folder>__<form>__<accession>__<doc>_mentions.csv        only that filing's mentions
      work/prepared/companies/<folder>__<form>__<accession>__<doc>_mentions.meta.json  form, date, filer, sec url ...
    The filer is the company that submitted the filing (usually NOT the listed company).
Filings without mentions are listed and skipped. The dumps and the finder output are only read.

Usage:
  python pipeline/prepare_filings.py databricks [--link] [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
from pathlib import Path

csv.field_size_limit(10**8)   # some Databricks context fields exceed the 131072 default
sys.path.insert(0, str(Path(__file__).resolve().parent))   # pipeline/: paths.py
import paths as px  # noqa: E402


def clean_filer(s: str) -> tuple[str, str]:
    """'Dynatrace, Inc.  (DT)  (CIK 0001773383)' -> ('Dynatrace, Inc.', '0001773383')."""
    m = re.search(r"CIK\s*(\d+)", s or "")
    name = re.sub(r"\s*\([^)]*\)", "", s or "").strip()
    return name, (m.group(1) if m else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("company", help="dump folder name, e.g. databricks or databricks_ncsr")
    ap.add_argument("--dumps", default=str(px.DUMPS))
    ap.add_argument("--data-dir", default=str(px.PREPARED))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--link", action="store_true", help="symlink the filings into data/pdfs instead of copying (large form sets)")
    a = ap.parse_args()
    dumps, data = Path(a.dumps), Path(a.data_dir)
    manifest = dumps / f"{a.company}_manifest.csv"
    outs = [d / f"{a.company}_mentions.csv" for d in px.finder_run_dirs(a.company) if (d / f"{a.company}_mentions.csv").exists()]
    if not manifest.exists() or not outs:
        raise SystemExit(f"manifest ({manifest}) or company-finder {a.company}_mentions.csv (under {px.FINDER_ROOT}) not found")
    mentions_csv = outs[0]
    run_dir = mentions_csv.parent
    inp = sorted((run_dir.parent.parent / "input").glob(f"{a.company}_input.csv"))
    canon = ""
    if inp:
        canon = next(csv.DictReader(open(inp[0], encoding="utf-8"))).get("canonical_name", "")
    man = {Path(r["file"]).name: r for r in csv.DictReader(open(manifest, encoding="utf-8"))}
    rows = list(csv.DictReader(open(mentions_csv, encoding="utf-8")))
    by_doc: dict[str, list[dict]] = {}
    for r in rows:
        by_doc.setdefault(Path(r["document"]).name, []).append(r)
    print(f"{a.company}: {len(rows)} kept mentions in {len(by_doc)} filing(s); manifest has {len(man)} filing(s); "
          f"company-finder run folder: {run_dir}")
    dropped = run_dir / f"{a.company}_dropped.csv"
    if dropped.exists():
        print(f"  dropped by the model earlier: {max(sum(1 for _ in open(dropped, encoding='utf-8')) - 1, 0)} row(s) (not used)")
    made = 0
    for name, rs in by_doc.items():
        mr = man.get(name)
        if not mr:
            print(f"  ! {name}: not in the manifest, skipped")
            continue
        src = dumps / a.company / mr["file"]
        if not src.exists():
            print(f"  ! {name}: file missing at {src}, skipped")
            continue
        filer, cik = clean_filer(mr["filer"])
        acc = mr["accession"]
        tag = f"{a.company}__{mr['form'].replace('/', '-')}__{acc}__{Path(name).stem}"   # stem: one accession can hold several documents
        pages = sorted({int(float(r['page'] or 0)) for r in rs})
        meta = {"company": canon or a.company, "company_folder": a.company, "form": mr["form"], "file_date": mr["file_date"],
                "accession": acc, "filer": filer, "filer_cik": cik, "document": name, "sec_url": mr["sec_url"],
                "mentions": len(rs), "pipeline_pages": pages,
                "note": "The filer submitted this filing; it is not the listed company. For .htm filings, pages are the filing's own "
                        "page breaks (the same numbering as the earlier run); the filing's images are not in the dump."}
        print(f"  {mr['form']:7} {mr['file_date']}  filer: {filer:28} {len(rs)} mention(s)  {name}")
        if a.dry_run:
            continue
        (data / "pdfs").mkdir(parents=True, exist_ok=True)
        (data / "companies").mkdir(parents=True, exist_ok=True)
        dest = data / "pdfs" / name
        if a.link:
            dest.unlink(missing_ok=True)
            dest.symlink_to(src.resolve())
        else:
            shutil.copy2(src, dest)
        with open(data / "companies" / f"{tag}_mentions.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rs[0].keys()))
            w.writeheader()
            w.writerows(rs)
        (data / "companies" / f"{tag}_mentions.meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        made += 1
    skipped = [n for n in man if n not in by_doc]
    if skipped:
        print(f"  no mentions in: {', '.join(skipped)}")
    print(f"{'would prepare' if a.dry_run else 'prepared'} {made if not a.dry_run else len(by_doc)} filing(s) into {data}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
