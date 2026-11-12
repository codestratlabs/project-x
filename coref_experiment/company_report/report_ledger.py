#!/usr/bin/env python3
"""Where every number in the report comes from. Reads the finished report (HTML), takes every number it prints, and finds
the record it was printed from: an evidence row (filing, table, row, printed header, unit), a fact sentence, a Form D
offering, an N-PORT row, a model table or an input table. A number with no record is listed as NO SOURCE: that is the gate.

    python report_ledger.py <company> <Title> <output folder>

Works from the report itself, not from the code that wrote it, so the workbook cannot drift from the report.
"""
from __future__ import annotations

import collections
import csv
import html as H
import re
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter as L

csv.field_size_limit(10**9)
co, title, dest = sys.argv[1], sys.argv[2], Path(sys.argv[3])
HERE = Path(__file__).parent
out = HERE / "out" / co
ev = out / "evidence"
ROOT = HERE.parents[2]


def money(v):
    a = abs(v)
    s = "−" if v < 0 else ""
    if a >= 1e9:
        return f"{s}${a / 1e9:.2f}B"
    if a >= 1e6:
        return f"{s}${a / 1e6:.1f}M"
    if a >= 1e3:
        return f"{s}${a / 1e3:.0f}K"
    return f"{s}${a:,.0f}"


def shows(v: float) -> set:
    """Every way the report prints a value."""
    out_ = {money(v), f"{v:,.0f}", f"${v:,.2f}", f"${v:,.0f}", f"{v:,.2f}", f"{v:,.1f}", f"{v:.1f}%", f"{v * 100:.1f}%", f"{v:.2f}%", f"{v * 100:.2f}%",
            f"{v:.0f}%", f"{v * 100:.0f}%", f"${v / 1e9:.1f}B", f"${v / 1e9:.2f}B", f"${v / 1e6:.0f}M", f"${v / 1e6:.2f}M", f"{v:.2f}", f"{v:.3f}", f"{v:.1f}x", f"{v:.2f}x"}
    return {x.replace("-", "−") if x.startswith("-") else x for x in out_} | out_


idx: dict = collections.defaultdict(list)


def add(v, src: dict):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return
    if v != v or v == 0:
        return
    for s in shows(v):
        idx[s].append(src)


def csv_rows(p: Path):
    return list(csv.DictReader(open(p, encoding="utf-8"))) if p.exists() else []


# 1. evidence: holdings, facts
for h in csv_rows(ev / "holdings_from_evidence.csv"):
    base = dict(kind=f"filing table ({h['tier']})", id=h["evidence_id"], what=f"{h['holder']} | {h['security']} | {h['position_type']} | as of {h['as_of_date']}",
                filing=f"{h['form']} {h['filing_date']} {h['filer']}", link=h["sec_url"], where=f"table {h['table']} row {h['row']}", header=h["header"], unit=h["unit"], raw=h["raw"])
    for k, lab in (("fair_value_usd", "fair value"), ("cost_usd", "cost"), ("shares", "shares"), ("par", "par"), ("commitment_usd", "commitment"),
                   ("price_per_share", "fair value / shares"), ("cost_per_share", "cost / shares")):
        add(h[k], {**base, "field": lab})
for f in csv_rows(ev / "facts_from_evidence.csv"):
    add(f["value"], dict(kind="sentence in a filing (rule)", id=f["fact_id"], field=f["metric"], what=f["printed"], filing=f"{f['form']} {f['filing_date']} {f['filer']}",
                         link=f["url"], where="sentence", header="", unit=f"scale word printed: {f['printed']}", raw=f["sentence"][:400]))
# 2. Form D
for i, r in enumerate(csv_rows(out / "formd_offerings.csv")):
    for k, v in r.items():
        add(v, dict(kind="Form D database", id=r.get("accession_number", f"row {i + 2}"), field=k, what=f"offering filed {r.get('filing_date', '')}", filing=r.get("submission_type", "D"),
                    link=f"https://www.sec.gov/Archives/edgar/data/{r.get('cik', '').lstrip('0')}/{r.get('accession_number', '').replace('-', '')}/", where=f"formd_offerings.csv row {i + 2}", header=k, unit="dollars", raw=""))
# 3. N-PORT
for i, r in enumerate(csv_rows(ROOT / "dumps" / f"{co}_nport" / "nport_holdings.csv")):
    for k in ("value_usd", "shares", "par_or_units", "filed_price_per_share"):
        add(r.get(k), dict(kind="Form N-PORT", id=r["accession"], field=k, what=f"{r['series']} | {r['title']} | {r['report_date']}", filing=f"{r['form']} {r['filing_date']}",
                           link=r["sec_url"], where=f"nport_holdings.csv row {i + 2}", header=k, unit="dollars / shares as filed (XML)", raw=""))
# 4. model and input tables: numbers the model computed, or records it read
for folder, kind in ((out / "model", "model table (computed)"), (HERE / "inputs" / co / "data", "input table (from raw records)")):
    for p in sorted(folder.glob("*.csv")):
        for i, r in enumerate(csv_rows(p)):
            link = next((v for k, v in r.items() if k and ("url" in k.lower() or "link" in k.lower()) and v), "")
            for k, v in r.items():
                add(v, dict(kind=kind, id=f"{p.name}:{i + 2}", field=k or "", what=" | ".join(str(x) for x in list(r.values())[:3])[:160], filing="", link=link,
                            where=f"{p.name} row {i + 2}, column {k}", header=k or "", unit="", raw=""))

# the report
rep = out / f"{co}_financials_year_by_year.html"
doc = rep.read_text(encoding="utf-8")
doc = re.sub(r"<(script|style)\b.*?</\1>", " ", doc, flags=re.S | re.I)
doc = re.sub(r"<svg\b.*?</svg>", " [chart] ", doc, flags=re.S | re.I)
TOK = re.compile(r"[−-]?\$\s?\d[\d,]*(?:\.\d+)?[BMK]?|\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|\b\d+\.\d+%?|\b\d+%|\b\d+(?:\.\d+)?x\b")
section, rows_out = "", []
pos = 0
for part in re.split(r"(<h[1-3][^>]*>.*?</h[1-3]>)", doc, flags=re.S | re.I):
    if re.match(r"<h[1-3]", part, re.I):
        section = H.unescape(re.sub(r"<[^>]+>", "", part)).strip()
        continue
    for el in re.split(r"</(?:tr|p|li|div)>", part, flags=re.I):
        t = re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", " | " if "<td" in el else " ", el))).strip(" |")
        for m in TOK.finditer(t):
            tok = m[0].replace(" ", "")
            if re.fullmatch(r"(19|20)\d\d", tok) or re.fullmatch(r"\d\.\d", tok) and t[max(0, m.start() - 8):m.start()].lower().endswith(("section ", "figure ")):
                continue
            hit = idx.get(tok, []) or idx.get(tok.replace("−", "-"), [])
            ctx = t[max(0, m.start() - 90):m.end() + 60]
            cat = "found" if hit else ("count or date (report)" if re.fullmatch(r"\d{1,3}(,\d{3})?", tok) and not tok.startswith("$") and len(tok) <= 5 else "NO SOURCE")
            best = sorted(hit, key=lambda s: ("filing" not in s["kind"] and "sentence" not in s["kind"], s["kind"]))[:1]
            b = best[0] if best else {}
            rows_out.append(dict(section=section[:80], shown=tok, context=ctx, status=cat, candidates=len(hit), kind=b.get("kind", ""), source_id=b.get("id", ""),
                                 field=b.get("field", ""), what=b.get("what", ""), filing=b.get("filing", ""), link=b.get("link", ""), where=b.get("where", ""),
                                 header=b.get("header", ""), unit=b.get("unit", ""), raw=b.get("raw", "")))

# workbook
dest.mkdir(parents=True, exist_ok=True)
wb = Workbook()
s = wb.active
s.title = "Summary"
navy = "1F3A5F"
cnt = collections.Counter(r["status"] for r in rows_out)
kinds = collections.Counter(r["kind"] for r in rows_out if r["status"] == "found")
s["A1"] = f"{title}: where every number in the report comes from"
s["A1"].font = Font(name="Arial", size=15, bold=True, color=navy)
s["A2"] = "Built from the finished report. Each number printed in it is matched to the record it was printed from."
lines = [("Numbers in the report", len(rows_out)), ("Found in a record", cnt["found"]), ("NO SOURCE (gate: must be 0)", cnt["NO SOURCE"]),
         ("Small counts and dates made by the report", cnt["count or date (report)"]), ("", "")] + [(f"  from {k}", v) for k, v in kinds.most_common()]
for i, (k, v) in enumerate(lines, 4):
    s.cell(i, 1, k).font = Font(name="Arial", size=10, bold=k.startswith("NO"))
    s.cell(i, 2, v).font = Font(name="Arial", size=10, bold=k.startswith("NO"), color="C00000" if k.startswith("NO") and v else "000000")
s.column_dimensions["A"].width = 52
s.column_dimensions["B"].width = 12
cols = [("section", "Report section", 26), ("shown", "Number as shown", 14), ("context", "Text around it in the report", 50), ("status", "Status", 14),
        ("kind", "Source kind", 22), ("field", "Field", 16), ("what", "Record", 40), ("filing", "Filing", 30), ("link", "Link", 14), ("where", "Where in the source", 26),
        ("header", "Printed column header", 30), ("unit", "Unit", 24), ("raw", "Row or sentence as printed", 60), ("source_id", "Source id", 16), ("candidates", "Records with this value", 10)]
for name, rr in (("Every number", rows_out), ("NO SOURCE", [r for r in rows_out if r["status"] == "NO SOURCE"])):
    w = wb.create_sheet(name)
    for i, (_, h, wd) in enumerate(cols, 1):
        c = w.cell(1, i, h)
        c.font = Font(name="Arial", bold=True, color="FFFFFF", size=10)
        c.fill = PatternFill("solid", fgColor=navy)
        c.alignment = Alignment(wrap_text=True, vertical="center")
        w.column_dimensions[L(i)].width = wd
    for j, r in enumerate(rr, 2):
        for i, (k, _, _) in enumerate(cols, 1):
            c = w.cell(j, i, r[k])
            c.font = Font(name="Arial", size=9, color="C00000" if r["status"] == "NO SOURCE" and k == "status" else "000000")
        lk = w.cell(j, 9)
        if str(lk.value or "").startswith("http"):
            lk.hyperlink, lk.value = lk.value, "Open"
            lk.font = Font(name="Arial", size=9, color="0563C1", underline="single")
    w.freeze_panes = "C2"
    w.auto_filter.ref = f"A1:{L(len(cols))}{len(rr) + 1}"
f = dest / f"{title}_Report_Number_Sources.xlsx"
wb.save(f)
print({"numbers": len(rows_out), **cnt}, f)
