#!/usr/bin/env python3
"""Verification workbook: every number and fact the rules accepted, with where it came from, plus everything held back and
why, plus the checks that were run. Values come from the evidence files only; nothing is typed here. No model is used.

    python verify_workbook.py <company> <Title> <output folder>
"""
from __future__ import annotations

import collections
import csv
import glob
import json
import random
import re
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter as L

csv.field_size_limit(10**9)
co, title, dest = sys.argv[1], sys.argv[2], Path(sys.argv[3])
HERE = Path(__file__).parent
ev = HERE / "out" / co / "evidence"
ROOT = HERE.parents[2]
rd = lambda n: list(csv.DictReader(open(ev / n, encoding="utf-8"))) if (ev / n).exists() else []   # noqa: E731
E, Hd, F, FR, RL, RR, RC, PR, HC_ = (rd(n) for n in ("evidence_rows.csv", "holdings_from_evidence.csv", "facts_from_evidence.csv", "facts_review.csv",
                                                      "relationships_from_evidence.csv", "relationships_review.csv", "recall_list.csv", "prose_sentences.csv",
                                                      "holdings_conflicts.csv"))
F_ = "Arial"
VAL = {v["evidence_id"]: v for v in (csv.DictReader(open(ev / "validation.csv", encoding="utf-8")) if (ev / "validation.csv").exists() else [])}
EM = {r["evidence_id"]: r for r in E}
for h in Hd:
    v, e_ = VAL.get(h["evidence_id"], {}), EM.get(h["evidence_id"], {})
    second = {"A N-PORT": "equal to Form N-PORT", "A tag": "iXBRL tag", "A XML": "N-CEN XML"}.get(h["tier"], "")
    h["checks"] = "; ".join(x for x in (
        "number printed in the row" if v.get("printed") == "ok" else "", "unit" if str(v.get("unit", "")).startswith("ok") else "",
        "date" if str(v.get("date", "")).startswith("ok") else "", "loan sums" if h["position_type"] == "funded_loan" and v.get("sums") == "ok" else "",
        "two readings agree" if (e_.get("methods") or "").startswith("two readings agree") else "single amount" if (e_.get("methods") or "").startswith("one") else "",
        second) if x)
BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff\ufffe\uffff]")
navy = "1F3A5F"


def sheet(wb, name, cols, rows, note=""):
    w = wb.create_sheet(name)
    r0 = 1
    if note:
        w.cell(1, 1, note).font = Font(name=F_, size=10, italic=True, color="555555")
        r0 = 2
    for i, (_, h, wd) in enumerate(cols, 1):
        c = w.cell(r0, i, h)
        c.font = Font(name=F_, bold=True, color="FFFFFF", size=10)
        c.fill = PatternFill("solid", fgColor=navy)
        c.alignment = Alignment(wrap_text=True, vertical="center")
        w.column_dimensions[L(i)].width = wd
    for j, r in enumerate(rows, r0 + 1):
        for i, (k, _, _) in enumerate(cols, 1):
            v = r.get(k, "")
            if k in NUM and v not in ("", None):
                try:
                    v = float(v)
                except ValueError:
                    pass
            if isinstance(v, str):
                v = BAD.sub(" ", v)
            c = w.cell(j, i, v if v != "" else None)
            c.font = Font(name=F_, size=9)
            if k in NUM:
                c.number_format = "#,##0.##"
            if k in ("sec_url", "url", "link") and str(v).startswith("http"):
                c.hyperlink, c.value = v, "Open"
                c.font = Font(name=F_, size=9, color="0563C1", underline="single")
    w.freeze_panes = w.cell(r0 + 1, 3)
    w.auto_filter.ref = f"A{r0}:{L(len(cols))}{len(rows) + r0}"
    return w


NUM = {"shares", "par", "cost_usd", "fair_value_usd", "commitment_usd", "price_per_share", "cost_per_share", "value", "qty_usd", "value_usd"}
HC = [("evidence_id", "Evidence id", 12), ("tier", "Proof", 10), ("holder", "Fund", 30), ("holder_source", "Fund name from", 22), ("position_type", "Type", 13),
      ("security", "Security", 10), ("shares", "Shares", 12), ("par", "Par / principal ($)", 14), ("cost_usd", "Cost ($)", 14), ("fair_value_usd", "Fair value ($)", 14),
      ("commitment_usd", "Commitment ($)", 13), ("price_per_share", "Value per share ($)", 11), ("acquisition_date", "Acquired", 11), ("as_of_date", "As of", 11),
      ("as_of_source", "Date from", 22), ("form", "Form", 8), ("filing_date", "Filed", 11), ("filer", "Filer", 28), ("sec_url", "Filing", 8), ("table", "Table", 9), ("row", "Row", 7),
      ("header", "Column header as printed", 34), ("unit", "Unit as printed", 26), ("raw", "Row as printed", 60), ("sources", "Same row in N filings", 8), ("checks", "Checks passed", 60)]
FC = [("fact_id", "Fact id", 12), ("metric", "Metric", 18), ("value", "Value", 16), ("unit", "Unit", 8), ("printed", "As printed", 16), ("date", "Date", 14),
      ("date_source", "Date from", 22), ("form", "Form", 8), ("filing_date", "Filed", 11), ("filer", "Filer", 28), ("url", "Filing", 8), ("sentence", "Sentence in the filing", 90), ("why", "Why in review", 34)]
RCOL = [("rel_id", "Id", 12), ("from_company", "From", 34), ("relation", "Relation", 12), ("to_company", "To", 18), ("basis", "Read from", 22), ("first_date", "First", 11),
        ("last_date", "Last", 11), ("rows", "Rows", 6), ("quote", "Row or quote as printed", 60), ("form", "Form", 8), ("filer", "Filer", 26), ("sec_url", "Filing", 8),
        ("check", "Check", 28), ("why", "Why in review", 34), ("evidence_ids", "Evidence ids", 30)]
ER = [("evidence_id", "Evidence id", 12), ("why", "Why in review", 30), ("form", "Form", 8), ("file_date", "Filed", 11), ("filer", "Filer", 28), ("as_of", "As of", 14),
      ("name", "Security as printed", 34), ("qty_usd", "Shares / par", 12), ("cost_usd", "Cost ($)", 13), ("value_usd", "Value ($)", 13), ("header", "Column header", 30),
      ("unit", "Unit", 22), ("raw", "Row as printed", 60), ("url", "Filing", 8), ("table", "Table", 9), ("row", "Row", 7)]

# recall list: each line not in evidence rows, sorted by what happened to it
prose_keys = {(p["accession"], re.sub(r"\W+", "", p["sentence"].lower())[:4000]) for p in PR}
for r in RC:
    k = re.sub(r"\W+", "", r["line"].lower())[:80]
    hit = any(a == r["accession"] and k and k[:60] in s for a, s in prose_keys) if r["kind"] == "name line" else False
    r["state"] = "kept as a prose sentence (facts or facts review)" if hit else "not read by a rule: a person reads it"

wb = Workbook()
s = wb.active
s.title = "Summary"
s.sheet_view.showGridLines = False
tiers = collections.Counter(r["tier"] for r in E)
why = collections.Counter(r["why"] for r in E if r["tier"] == "C review")
s["A1"] = f"{title}: verification of numbers and facts (rules only, no AI)"
s["A1"].font = Font(name=F_, size=15, bold=True, color=navy)
s["A2"] = "Every number is read by rule from a filing, with the printed column header, unit and date. N-PORT and iXBRL only confirm or flag; they never set a value."
s["A2"].font = Font(name=F_, size=10, color="555555")
cf = {json.load(open(m))["accession"] for m in glob.glob(str(HERE.parent / "coref_verifier" / "data" / "companies" / f"{co}*__*_mentions.meta.json"))}
ours = {r["accession"] for r in E} | {p["accession"] for p in PR}
old = ev / "evidence_rows_prev.csv"
def date_test() -> str:
    """Rows whose value matched N-PORT by the old nearest-date rule are dated independently by N-PORT; the new rule must give the same date."""
    if not old.exists():
        return "not run"
    k = lambda r: (r["accession"], r["table"], r["row"], r["name"][:40])   # noqa: E731
    o = {k(r): r["report_date"] for r in csv.DictReader(open(old, encoding="utf-8")) if r["tier"] == "A N-PORT"}
    c = collections.Counter()
    for r in E:
        if k(r) in o:
            c["same" if o[k(r)] == r["report_date"] else "none" if not r["report_date"] else "different"] += 1
    return f"{c['same']} / {c['different']} / {c['none']}"


blocks = [
    ("What was accepted", [("Holdings (proof A or B, dated)", len(Hd)), ("  proof A: equal to a Form N-PORT row, an iXBRL tag or an N-CEN XML field", sum(h["tier"].startswith("A") for h in Hd)),
                           ("  proof B: column header and unit printed, no second source", sum(h["tier"] == "B stated" for h in Hd)),
                           ("Facts from sentences and N-CEN (rule)", len(F)), ("Relationships from holdings tables (rule)", len(RL))]),
    ("What was held back (not used until a person reads it)", [("Table rows in review", tiers["C review"])] + [(f"  {k}", v) for k, v in why.most_common(9)] +
     [("Numbers in sentences not tied to the company by rule", len(FR)), ("Relationships from the earlier model run (not used)", len(RR)),
      ("Name lines in the context windows not in the evidence", sum(r["kind"] == "name line" for r in RC))]),
    ("Checks run", [("Filings where the string match found the company and this scan did not", len(cf - ours)),
                    ("Date test, rows with a second source: value and shares equal the filer's own N-PORT row of the same date", sum(1 for r in E if r["tier"] == "A N-PORT")),
                    ("Date test, rows with no second source: same shares and value found in N-PORT at another date (moved to review)", sum(1 for r in E if r["why"].startswith("same shares and value as an N-PORT row of another date"))),
                    ("Rows equal to the N-PORT row of another registrant (number proven, fund named as the filer)", sum(1 for r in E if r.get("nport_note"))),
                    ("Negative numbers: printed (44) or -44, or iXBRL sign attribute, read as negative", "yes"),
                    ("Positions where two filings give different numbers (both moved to review)", len(HC_)),
                    ("Accepted holdings whose two readings (column position, right-aligned) agree", sum(1 for h in Hd if "two readings agree" in h.get("checks", ""))),
                    ("Accepted holdings with a single amount in the row (nothing to mix up)", sum(1 for h in Hd if "single amount" in h.get("checks", ""))),
                    ("Rows moved to review because the two readings differ", sum(1 for r in E if r["why"].startswith("two readings differ"))),
                    ("Every accepted holding checked against the filing text (printed, unit, date, loan sums): failed and moved to review", len(rd("holdings_failed_validation.csv"))),
                    ("Rows with no date found (left out)", sum(r["why"] == "no date found for the table" for r in E)),
                    ("Filings in the manifest scanned", len({r["accession"] for r in E} | {p["accession"] for p in PR}))]),
    ("Open", [("Gold check", "a person ticks the Gold check workbook (rows picked at random by type and fund family)"),
              ("Review rows", "C review, facts review and the context-window lines are read by a person or fixed by a rule"),
              ("Form N-PORT disagreements", f"{why['same shares as an N-PORT row, different value']} rows: read in the filing")]),
]
row = 4
for head, items in blocks:
    s.cell(row, 1, head).font = Font(name=F_, size=12, bold=True, color=navy)
    row += 1
    for k, v in items:
        s.cell(row, 1, k).font = Font(name=F_, size=10)
        s.cell(row, 2, v).font = Font(name=F_, size=10)
        row += 1
    row += 1
s.column_dimensions["A"].width = 78
s.column_dimensions["B"].width = 70
sheet(wb, "Holdings", HC, Hd, "One row per position per date. Proof A = equal to Form N-PORT or an iXBRL tag; B = header and unit printed in the filing.")
sheet(wb, "Facts", FC[:-1], F, "Numbers from sentences (scale word printed next to the number) and Form N-CEN XML fields.")
sheet(wb, "Relationships", RCOL[:-2] + RCOL[-1:], RL, "A fund whose holdings table lists the company: invests_in (shares) or lends_to (loans).")
sheet(wb, "Review - table rows", ER, [r for r in E if r["tier"] == "C review"], "Not used until a person reads the filing.")
VF = rd("holdings_failed_validation.csv")
sheet(wb, "Review - failed checks", [("evidence_id", "Evidence id", 12), ("why", "Check that failed", 40), ("form", "Form", 8), ("filer", "Filer", 30), ("as_of", "As of", 11),
                                     ("name", "Security as printed", 34), ("raw", "Row as printed", 70), ("url", "Filing", 8)], VF,
      "Rows that failed validate_all.py (number not printed in the row, unit or date not printed, loan amounts that do not add up). Not used.")
sheet(wb, "Review - conflicts", HC + [("why", "Why in review", 34)], HC_, "Two or more filings give the same position different numbers. Not used.")
sheet(wb, "Review - facts", FC, FR, "Numbers in sentences that name the company, not tied to it by rule. Not used.")
sheet(wb, "Review - relationships", RCOL, RR, "Earlier model rows. Not used in a rule-only run.")
sheet(wb, "Review - context lines", [("kind", "Kind", 14), ("state", "State", 30), ("form", "Form", 8), ("filed", "Filed", 11), ("filer", "Filer", 30), ("accession", "Accession", 21), ("line", "Line in the filing", 110)], RC,
      "Lines with a number that name the company in the context-window workbook and are not an evidence row.")
dest.mkdir(parents=True, exist_ok=True)
f1 = dest / f"{title}_Verification.xlsx"
wb.save(f1)

# gold check: random rows by proof, reader and fund family; every fact
random.seed(int(sys.argv[4]) if len(sys.argv) > 4 else 20261108)   # a new seed gives a fresh sample after fixes
fam = collections.defaultdict(list)
for h in Hd:                                       # rows with no second source are drawn three times as often
    k = (h["tier"], h["position_type"], re.sub(r"\W.*", "", h["filer"]).upper(), "proven by another row" in h["unit"])
    fam[k].append(h)
keys_b = [k for k in fam if k[0] == "B stated"]
gold, keys, i = [], sorted(fam) + sorted(keys_b) * 2, 0
while len(gold) < 200 - len(F) and any(fam[k] for k in keys):
    k = keys[i % len(keys)]
    if fam[k]:
        x = fam[k].pop(random.randrange(len(fam[k])))
        gold.append({"kind": "holding", "id": x["evidence_id"], "fund": x["holder"], "what": f"{x['position_type']} {x['security']}".strip(), "as_of": x["as_of_date"],
                     "n1": x["shares"] or x["par"], "n2": x["cost_usd"], "n3": x["fair_value_usd"] or x["commitment_usd"], "header": x["header"], "unit": x["unit"],
                     "raw": x["raw"], "url": x["sec_url"], "form": x["form"], "filed": x["filing_date"]})
    i += 1
gold += [{"kind": "fact", "id": f["fact_id"], "fund": f["filer"], "what": f["metric"], "as_of": f["date"], "n1": "", "n2": "", "n3": f["value"], "header": f["printed"],
          "unit": f["unit"], "raw": f["sentence"], "url": f["url"], "form": f["form"], "filed": f["filing_date"]} for f in F]
g = Workbook()
w = g.active
w.title = "Gold check"
w["A1"] = f"{title} gold check: open the filing and compare. Fill the yellow columns only (Y or N, and a note for N)."
w["A1"].font = Font(name=F_, size=12, bold=True, color=navy)
GC = [("n", "#", 5), ("kind", "Kind", 8), ("id", "Id", 12), ("fund", "Fund / filer", 30), ("what", "What", 22), ("as_of", "As of", 12), ("n1", "Shares or par", 13),
      ("n2", "Cost ($)", 13), ("n3", "Value ($)", 14), ("header", "Header or printed", 30), ("unit", "Unit", 22), ("raw", "Row or sentence as printed", 60),
      ("form", "Form", 8), ("filed", "Filed", 11), ("url", "Filing", 8), ("ok_num", "Numbers right? Y/N", 10), ("ok_date", "Date right? Y/N", 10), ("ok_fund", "Fund right? Y/N", 10), ("note", "Note", 30)]
for i, (_, h, wd) in enumerate(GC, 1):
    c = w.cell(3, i, h)
    c.font = Font(name=F_, bold=True, color="FFFFFF", size=10)
    c.fill = PatternFill("solid", fgColor=navy)
    c.alignment = Alignment(wrap_text=True)
    w.column_dimensions[L(i)].width = wd
for j, r in enumerate(gold, 4):
    r["n"] = j - 3
    for i, (k, _, _) in enumerate(GC, 1):
        v = r.get(k, "")
        if k in ("n1", "n2", "n3") and v not in ("", None):
            v = float(v)
        if isinstance(v, str):
            v = BAD.sub(" ", v)
        c = w.cell(j, i, v if v != "" else None)
        c.font = Font(name=F_, size=9)
        if k in ("n1", "n2", "n3"):
            c.number_format = "#,##0.##"
        if k == "url" and str(v).startswith("http"):
            c.hyperlink, c.value = v, "Open"
            c.font = Font(name=F_, size=9, color="0563C1", underline="single")
        if k in ("ok_num", "ok_date", "ok_fund", "note"):
            c.fill = PatternFill("solid", fgColor="FFF2B3")
w.freeze_panes = "E4"
f2 = dest / f"{title}_Gold_Check.xlsx"
g.save(f2)
print(f1, f2, {"holdings": len(Hd), "facts": len(F), "relationships": len(RL), "review rows": tiers["C review"], "gold": len(gold),
               "context lines": collections.Counter(r["state"] for r in RC)})
