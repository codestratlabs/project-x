#!/usr/bin/env python3
"""Two workbooks from the evidence files, nothing typed by hand:

    <Title>_Verified_Data.xlsx     only rows that passed every check: holdings, facts, relationships
    <Title>_Evidence_Clashes.xlsx  only rows held back, each with the reason in plain words

    python final_excels.py <company> <Title> <output folder>
"""
from __future__ import annotations

import collections
import csv
import re
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter as L

csv.field_size_limit(10**9)
co, title, dest = sys.argv[1], sys.argv[2], Path(sys.argv[3])
ev = Path(__file__).parent / "out" / co / "evidence"
rd = lambda n: list(csv.DictReader(open(ev / n, encoding="utf-8"))) if (ev / n).exists() else []   # noqa: E731
E, Hd, F, RL, VF, HC = (rd(n) for n in ("evidence_rows.csv", "holdings_from_evidence.csv", "facts_from_evidence.csv",
                                        "relationships_from_evidence.csv", "holdings_failed_validation.csv", "holdings_conflicts.csv"))
EM = {r["evidence_id"]: r for r in E}
LOC = {r["id"]: r for r in rd("locator.csv")}
for r in E:
    l_ = LOC.get(r["evidence_id"], {})
    r.update(page=l_.get("page", ""), search_text=l_.get("search_text", ""), times=l_.get("times_in_filing", ""), where=l_.get("where", ""))
FONT, NAVY = "Arial", "1F3A5F"
BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff￾￿]")
NUM = {"shares", "par", "cost_usd", "fair_value_usd", "commitment_usd", "price_per_share", "value", "qty_usd", "value_usd", "nport_value"}


def sheet(wb, name, cols, rows, note):
    w = wb.create_sheet(name)
    w.sheet_properties.tabColor = NAVY
    for i, (_, h, wd) in enumerate(cols, 1):
        c = w.cell(1, i, h)
        c.font = Font(name=FONT, bold=True, color="FFFFFF", size=10)
        c.fill = PatternFill("solid", fgColor=NAVY)
        c.alignment = Alignment(wrap_text=True, vertical="center")
        w.column_dimensions[L(i)].width = wd
    w.row_dimensions[1].height = 30
    for j, r in enumerate(rows, 2):
        for i, (k, _, _) in enumerate(cols, 1):
            v = r.get(k, "")
            if k in NUM and v not in ("", None):
                v = float(v)
            if isinstance(v, str):
                v = BAD.sub(" ", v)
            c = w.cell(j, i, v if v != "" else None)
            c.font = Font(name=FONT, size=9)
            if k in NUM:
                c.number_format = "#,##0.##"
            if k in ("sec_url", "url") and str(v).startswith("http"):
                c.hyperlink, c.value = v, "Open filing"
                c.font = Font(name=FONT, size=9, color="0563C1", underline="single")
    w.freeze_panes = "C2"
    w.auto_filter.ref = f"A1:{L(len(cols))}{len(rows) + 1}"
    return note


def summary(wb, head, lines):
    s = wb.active
    s.title = "Summary"
    s.sheet_view.showGridLines = False
    s["A1"] = head
    s["A1"].font = Font(name=FONT, size=15, bold=True, color=NAVY)
    r = 3
    for a, b in lines:
        s.cell(r, 1, a).font = Font(name=FONT, size=10, bold=b == "")
        if b != "":
            s.cell(r, 2, b).font = Font(name=FONT, size=10)
        r += 1
    s.column_dimensions["A"].width = 70
    s.column_dimensions["B"].width = 90


# ---- verified data
for h in Hd + F:
    l_ = LOC.get(h.get("evidence_id") or h.get("fact_id"), {})
    h.update(page=l_.get("page", ""), search_text=l_.get("search_text", ""), times=l_.get("times_in_filing", ""), where=l_.get("where", ""))
for h in Hd:
    e = EM.get(h["evidence_id"], {})
    h["proof"] = {"A N-PORT": "Same as the fund's Form N-PORT filing", "A tag": "Same as the iXBRL tag in the filing",
                  "A XML": "Form N-CEN XML field", "B stated": "Read from the printed table"}.get(h["tier"], h["tier"])
    h["read_check"] = "two readings agree" if (e.get("methods") or "").startswith("two readings agree") else "one amount in the row" if (e.get("methods") or "").startswith("one") else "tag / XML"
wb = Workbook()
HC_ = [("holder", "Fund", 32), ("position_type", "Type", 14), ("security", "Security", 11), ("shares", "Shares", 12), ("par", "Loan principal ($)", 15),
       ("cost_usd", "Cost ($)", 15), ("fair_value_usd", "Fair value ($)", 15), ("commitment_usd", "Undrawn commitment ($)", 15), ("price_per_share", "Value per share ($)", 11),
       ("acquisition_date", "Acquired", 11), ("as_of_date", "As of", 11), ("proof", "Proof", 30), ("read_check", "Column check", 18), ("form", "Form", 8),
       ("filing_date", "Filed", 11), ("filer", "Filer", 30), ("sec_url", "Filing", 11), ("page", "Page", 9), ("search_text", "Search this (Ctrl+F)", 16), ("times", "Times it appears", 8), ("where", "Where", 18), ("header", "Column header as printed", 36), ("unit", "Unit as printed", 28),
       ("raw", "Row as printed in the filing", 70), ("evidence_id", "Id", 12)]
FC_ = [("metric", "What", 20), ("printed", "As printed", 16), ("value", "Value", 16), ("unit", "Unit", 8), ("date", "Date", 16), ("form", "Form", 8),
       ("filing_date", "Filed", 11), ("filer", "Filer", 30), ("url", "Filing", 11), ("page", "Page", 9), ("search_text", "Search this (Ctrl+F)", 16), ("sentence", "Sentence in the filing", 100), ("fact_id", "Id", 12)]
RC_ = [("from_company", "Fund", 36), ("relation", "Relation", 12), ("to_company", "Company", 18), ("first_date", "First seen", 11), ("last_date", "Last seen", 11),
       ("rows", "Holdings rows", 9), ("proof_as_of", "Proof row as of", 11), ("form", "Form", 8), ("filing_date", "Filed", 11), ("filer", "Filer", 30), ("sec_url", "Filing", 11),
       ("page", "Page", 9), ("search_text", "Search this (Ctrl+F)", 16), ("times", "Times it appears", 8), ("where", "Where", 18),
       ("proof_row", "Holdings row that proves it, as printed", 70), ("proof_id", "Holding id", 12), ("rel_id", "Id", 12)]
loans = ("funded_loan", "unfunded_commitment")
for r in RL:
    # the holding row that proves the relationship: the fund's latest row of that kind, with its filing, page and search text
    kind = (lambda h: h["position_type"] in loans) if r["relation"] == "lends_to" else (lambda h: h["position_type"] not in loans)
    hs = sorted((h for h in Hd if h["holder"] == r["from_company"] and kind(h)), key=lambda h: (h["as_of_date"], h["filing_date"]))
    if hs:
        b = hs[-1]
        r.update(proof_row=b["raw"], proof_as_of=b["as_of_date"], form=b["form"], filer=b["filer"], filing_date=b["filing_date"], sec_url=b["sec_url"],
                 page=b.get("page", ""), search_text=b.get("search_text", ""), times=b.get("times", ""), where=b.get("where", ""), proof_id=b["evidence_id"])
    r["relation"] = {"invests_in": "investor", "lends_to": "lender"}.get(r["relation"], r["relation"])
sheet(wb, "Holdings", HC_, Hd, "")
sheet(wb, "Facts", FC_, F, "")
sheet(wb, "Relationships", RC_, RL, "")
pt = collections.Counter(h["position_type"] for h in Hd)
summary(wb, f"{title}: verified data", [
    ("Every row in this workbook passed every check below. Rows that did not are in the Evidence Clashes workbook.", ""), ("", ""),
    ("What is in it", ""), ("Holdings (one row per fund, security and date)", len(Hd)),
    ("  shares", pt["equity"]), ("  loans", pt["funded_loan"]), ("  undrawn loan commitments", pt["unfunded_commitment"]), ("  interests in SPVs that hold the company", pt["spv_interest"]),
    ("  also equal to the fund's Form N-PORT filing, an iXBRL tag or N-CEN XML", sum(h["tier"].startswith("A") for h in Hd)),
    ("Facts (from sentences, with the scale word printed next to the number; and N-CEN purchases)", len(F)),
    ("Relationships (a fund that holds the company's shares = investor; its loan = lender)", len(RL)), ("", ""),
    ("Checks every holding passed", ""),
    ("1  Coverage", "every downloaded filing naming the company was scanned; none missed against the string-match run and an EDGAR search"),
    ("2  Number printed", "each number is printed in that exact row of the filing (the filing is reopened and the row read again)"),
    ("3  Unit", "multiplied by 1,000 or 1,000,000 only when 'thousands', '(000)' or 'millions' is printed with the table"),
    ("4  Date", "the as-of date is printed above the table, on the cover, or in the filing's iXBRL tag"),
    ("5  Column", "the row is read two ways (by column position, and counting from the right of the header) and both give the same numbers"),
    ("6  Loan sums", "a loan's principal, cost and fair value are within 35% of each other"),
    ("7  Price", "value per share is within 3x of the funds' Form N-PORT price for that month"),
    ("8  Same fund", "a Form N-PORT match counts only from the same registrant"),
    ("9  No clash", "no other filing gives the same position a different number")])
dest.mkdir(parents=True, exist_ok=True)
f1 = dest / f"{title}_Verified_Data.xlsx"
wb.save(f1)

# ---- clashes
PLAIN = [("same shares as an N-PORT row, different value", "Disagrees with Form N-PORT", "Same shares as the fund's N-PORT filing but a different value"),
         ("two readings differ", "Column not certain", "The two readings of the row put the numbers in different columns"),
         ("amount cells at the end of the row", "Column not certain", "The row has more amounts than the header has columns"),
         ("bracketed number", "Column not certain", "A number in brackets could be a footnote or a negative amount"),
         ("read one way only", "Column not certain", "Several amounts in the row and only one way to read them"),
         ("no unit printed", "Unit not printed", "No unit printed with the table and no second source to confirm it"),
         ("per-share value out of range", "Price out of range", "Value per share far from the funds' N-PORT price that month"),
         ("same shares and value as an N-PORT row of another date", "Date not certain", "The same row appears in N-PORT at another date"),
         ("no date found", "Date not printed", "No as-of date printed for the table"),
         ("no column header", "No header", "The table has no printed column header"),
         ("no number under a known column", "No header", "Numbers in the row sit under no recognised column"),
         ("pdf", "PDF line", "A PDF line whose columns cannot be told apart by rule")]
rows = []
for r in E:
    if r["tier"] != "C review":
        continue
    w = r["why"].lower()
    cat, plain = next(((c, p) for k, c, p in PLAIN if k.lower() in w), ("Other", r["why"]))
    rows.append({**r, "category": cat, "reason": plain, "detail": r["why"]})
for r in VF:
    e = EM.get(r["evidence_id"], {})
    rows.append({**e, "category": "Failed a check", "reason": r["why"], "detail": r["why"]})
for h in HC:
    e = EM.get(h["evidence_id"], {})
    rows.append({**e, "category": "Filings disagree", "reason": "Another filing gives this position a different number", "detail": h.get("why", "")})
wb = Workbook()
CC = [("category", "Clash", 22), ("reason", "Why it is held back", 50), ("form", "Form", 8), ("file_date", "Filed", 11), ("filer", "Filer", 30), ("report_date", "As of", 11),
      ("name", "Security as printed", 36), ("qty_usd", "Shares / principal", 13), ("cost_usd", "Cost ($)", 14), ("value_usd", "Value ($)", 14), ("header", "Column header as printed", 34),
      ("unit", "Unit", 24), ("raw", "Row as printed in the filing", 70), ("url", "Filing", 11), ("page", "Page", 9), ("search_text", "Search this (Ctrl+F)", 16), ("times", "Times it appears", 8), ("detail", "Detail", 40), ("evidence_id", "Id", 12)]
by = collections.defaultdict(list)
for r in rows:
    by[r["category"]].append(r)
for cat in sorted(by, key=lambda c: -len(by[c])):
    sheet(wb, cat[:31], CC, by[cat], "")
summary(wb, f"{title}: evidence clashes", [
    ("Rows the script found in the filings but could not prove. None of them is in the Verified Data workbook.", ""), ("", ""),
    ("Clash", "")] + [(f"{c}  ({n} rows)", next(p for k, c2, p in PLAIN if c2 == c) if any(c2 == c for _, c2, _ in PLAIN) else
                       "Failed one of the checks on the printed filing" if c == "Failed a check" else "Two filings give the same position different numbers" if c == "Filings disagree" else "")
                      for c, n in sorted(((c, len(v)) for c, v in by.items()), key=lambda x: -x[1])] + [("Total", len(rows))])
f2 = dest / f"{title}_Evidence_Clashes.xlsx"
wb.save(f2)
print(f1, f2, {"holdings": len(Hd), "facts": len(F), "relationships": len(RL), "clashes": len(rows)}, {c: len(v) for c, v in by.items()})
