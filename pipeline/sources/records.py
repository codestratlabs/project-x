#!/usr/bin/env python3
"""Structured records of one company, read by rule (no model, no value typed by hand):

  N-PORT price marks   work/inputs/<co>/data/nport_price_monthly.csv   median price per share per month across funds (the
                       evidence steps use it to check a holding's per-share value)
  DOL Form 5500        work/inputs/<co>/data/dol_form5500.csv          plan participants (+ plan_participants.csv, dol_5500_sch_h.csv)
  Form D offerings     work/out/<co>/formd_offerings.csv               the company's own Form D notices, latest filing per offering

    python pipeline/sources/records.py <company> --cik <10-digit CIK> [--formd-db FILE] [--employee-dir DIR]

Each source the company does not have is reported and skipped.
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import json
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # pipeline/: paths.py
import paths as px  # noqa: E402


def write(out: Path, name: str, header: list, body: list) -> int:
    (out / "data").mkdir(parents=True, exist_ok=True)
    with open(out / "data" / name, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(body)
    return len(body)


def write_csv(path: Path, rows: list[dict], cols: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        wr.writeheader()
        wr.writerows(rows)


def as_date(x) -> dt.date | None:
    if x is None or x == "":
        return None
    if isinstance(x, dt.datetime):
        return x.date()
    if isinstance(x, dt.date):
        return x
    m = re.match(r"(\d{4})-(\d{2})(?:-(\d{2}))?", str(x))
    if not m:
        return None
    try:
        return dt.date(int(m[1]), int(m[2]), int(m[3] or 1))
    except ValueError:
        return None


# ------------------------------------------------------------------ N-PORT price marks
def marks(nport_csv: str, cfg: dict, out: Path) -> dict:
    by = collections.defaultdict(list)
    day, ratio = cfg.get("split_date", "0000"), cfg.get("split_ratio", 1)
    with open(nport_csv, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["position_type"] == "equity" and r["filed_price_per_share"]:
                r["adj"] = float(r["filed_price_per_share"]) / (ratio if r["report_date"] < day else 1)
                by[r["report_date"][:7]].append(r)
    body = []
    for m in sorted(by):
        p = [x["adj"] for x in by[m]]
        body.append([m, len(p), len({x["series_id"] or x["cik"] for x in by[m]}), round(statistics.median(p), 4), round(min(p), 4), round(max(p), 4)])
    return {"nport_price_monthly.csv": write(out, "nport_price_monthly.csv", ["month", "positions", "funds", "median_price", "min_price", "max_price"], body)}


# ------------------------------------------------------------------ DOL Form 5500
def form5500(emp: Path, cfg: dict, out: Path) -> dict:
    co = cfg["company"]["employee_company"]
    rd = lambda n: [r for r in csv.DictReader(open(emp / n, encoding="utf-8", newline="")) if r.get("company") == co]      # noqa: E731
    f = rd("form5500_rows.csv")
    latest: dict = {}
    for r in f:
        k = (r["FORM_PLAN_YEAR_BEGIN_DATE"][:4], r["SPONS_DFE_PN"])
        latest[k] = r if k not in latest or r["ACK_ID"] > latest[k]["ACK_ID"] else latest[k]
    n1 = write(out, "dol_form5500.csv", ["FORM_PLAN_YEAR_BEGIN_DATE", "SPONS_DFE_PN", "TOT_ACT_PARTCP_BOY_CNT", "TOT_ACTIVE_PARTCP_CNT", "PLAN_NAME", "ACK_ID"],
               [[r["FORM_PLAN_YEAR_BEGIN_DATE"], r["SPONS_DFE_PN"], r["TOT_ACT_PARTCP_BOY_CNT"], r["TOT_ACTIVE_PARTCP_CNT"], r.get("PLAN_NAME", ""), r.get("ACK_ID", "")]
                for k, r in sorted(latest.items())])
    pp = {}
    for k, r in latest.items():
        if r["SPONS_DFE_PN"] == cfg["k401_plan"]:
            pp[int(k[0])] = [k[0], cfg["k401_plan"], r["TOT_ACT_PARTCP_BOY_CNT"], r["TOT_ACTIVE_PARTCP_CNT"]]
    for r in rd("form5500sf_rows.csv"):
        y = int(r["SF_PLAN_YEAR_BEGIN_DATE"][:4]) if "SF_PLAN_YEAR_BEGIN_DATE" in r else None
        if y and y not in pp and r.get("SF_PLAN_NUM", r.get("SF_PN")) == cfg["k401_plan"]:
            pp[y] = [str(y), cfg["k401_plan"], r.get("SF_TOT_ACT_PARTCP_BOY_CNT", ""), r.get("SF_TOT_ACT_PARTCP_EOY_CNT", "")]
    n2 = write(out, "plan_participants.csv", ["plan_year", "plan_number", "active_boy", "active_eoy"], [pp[y] for y in sorted(pp)])
    w_ = [r for r in csv.DictReader(open(emp.parent / "data" / "welfare_plans_by_year.csv", encoding="utf-8", newline="")) if r["company"] == co]
    write(out, "welfare_plans.csv", ["plan_year", "plan_number", "plan_kind", "active_participants_eoy"], [[r["plan_year"], r["plan_number"], r["plan_kind"], r["active_participants_eoy"]] for r in w_])
    h = [r for r in rd("schedule_h_rows.csv") if r.get("SCH_H_PN") == cfg["k401_plan"]]
    last: dict = {}
    for r in h:
        y = r["SCH_H_PLAN_YEAR_BEGIN_DATE"][:4]
        last[y] = r if y not in last or r["ACK_ID"] > last[y]["ACK_ID"] else last[y]
    n3 = write(out, "dol_5500_sch_h.csv", ["SCH_H_PLAN_YEAR_BEGIN_DATE", "PARTICIPANT_CONTRIB_AMT", "EMPLR_CONTRIB_INCOME_AMT", "NET_ASSETS_EOY_AMT"],
               [[r["SCH_H_PLAN_YEAR_BEGIN_DATE"], r["PARTICIPANT_CONTRIB_AMT"], r["EMPLR_CONTRIB_INCOME_AMT"], r["NET_ASSETS_EOY_AMT"]] for y, r in sorted(last.items())])
    return {"dol_form5500.csv": n1, "plan_participants.csv": n2, "dol_5500_sch_h.csv": n3}


# ------------------------------------------------------------------ Form D offerings
def formd_offerings(db_path: Path, cik: str, out_csv: Path) -> dict:
    """The company's Form D filings from the Form D database (tools/build_formd_db.py). An amendment restates the whole
    offering: the latest filing of each offering (file number, else accession) is kept."""
    import duckdb

    db = duckdb.connect(str(db_path), read_only=True)
    if not db.execute("SELECT 1 FROM main.companies WHERE cik = ?", [cik]).fetchall():
        raise SystemExit(f"CIK {cik} is not in {db_path}")
    cur = db.execute("""SELECT accession_number, filing_date, submission_type, sale_date, file_num, is_amendment,
        previous_accession_number, is_equity, is_debt, is_option_to_acquire, is_business_combination, other_type_desc,
        revenue_range, total_offering_amount, total_amount_sold, total_remaining, total_investors, minimum_investment,
        sales_amt_clarification, gross_proceeds_clarification, year_of_inc, issuer_name
        FROM main.filings WHERE cik = ? ORDER BY filing_date""", [cik])
    cols = [x[0] for x in cur.description]
    filings = [dict(zip(cols, r)) for r in cur.fetchall()]
    offerings: dict = {}
    for f in filings:
        key = f["file_num"] or f["previous_accession_number"] or f["accession_number"]
        offerings[key] = f
    offs = sorted(offerings.values(), key=lambda f: (as_date(f["sale_date"]) or as_date(f["filing_date"])))
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    write_csv(out_csv, offs, list(offs[0].keys()) if offs else [])
    return {out_csv.name: len(offs)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("company")
    ap.add_argument("--cik", required=True, help="the company's own CIK (Form D)")
    ap.add_argument("--formd-db", default=str(px.FORMD_DB))
    ap.add_argument("--employee-dir", default=str(px.DUMPS / "employee_docs" / "raw"), help="DOL Form 5500 rows (form5500_rows.csv ...)")
    a = ap.parse_args()
    co = a.company
    cfg = json.loads(px.config_path(co).read_text(encoding="utf-8"))
    inputs, np_csv = px.INPUTS / co, px.DUMPS / f"{co}_nport" / "nport_holdings.csv"
    steps = [("N-PORT price marks", lambda: marks(str(np_csv), cfg, inputs)),
             ("DOL Form 5500", lambda: form5500(Path(a.employee_dir), cfg, inputs)),
             ("Form D offerings", lambda: formd_offerings(Path(a.formd_db), a.cik.zfill(10), px.OUT / co / "formd_offerings.csv"))]
    missing = []
    for name, fn in steps:
        try:
            print(f"{name}: {fn()}", flush=True)
        except Exception as exc:                       # a source this company does not have
            missing.append(name)
            print(f"MISSING {name}: {type(exc).__name__}: {exc}", flush=True)
    print("not built:", missing or "none", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
