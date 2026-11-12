#!/usr/bin/env python3
"""Write the evidence check to one workbook: summary, verified, filing-confirmed, failed, and numbers in filings that are not in the evidence."""
import collections, csv, re, shutil, sys, zipfile
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.workbook.properties import CalcProperties
csv.field_size_limit(10**8)
co, title, dest = sys.argv[1], sys.argv[2], sys.argv[3]
ev = Path(__file__).parent / "out" / co / "evidence"
R = list(csv.DictReader(open(ev / "number_check.csv", encoding="utf-8")))
U = list(csv.DictReader(open(ev / "not_in_evidence.csv", encoding="utf-8")))
F, navy = "Arial", "1F3A5F"
thin = Side(style="thin", color="D5DCE3")
wb = Workbook()
s = wb.active
s.title = "Summary"
COLS = [("kind", "Type", 10), ("name", "Holder / metric", 34), ("item", "Security / statement", 34), ("shares", "Shares", 13), ("cost", "Cost ($)", 15), ("fair_value", "Value ($)", 16), ("commitment", "Commitment ($)", 15), ("as_of", "As of", 11),
        ("second_source", "Second source", 18), ("reason", "Why it failed", 48), ("form", "Form", 9), ("filer", "Filer", 30), ("filing_date", "Filed", 11), ("evidence", "Line in the filing", 70), ("url", "Filing link", 30)]


def sheet(name, rows, cols):
    w = wb.create_sheet(name)
    for i, (_, h, wd) in enumerate(cols, 1):
        c = w.cell(1, i, h)
        c.font = Font(name=F, bold=True, color="FFFFFF", size=10)
        c.fill = PatternFill("solid", fgColor=navy)
        c.alignment = Alignment(vertical="center", wrap_text=True)
        w.column_dimensions[chr(64 + i)].width = wd
    for j, r in enumerate(rows, 2):
        for i, (k, _, _) in enumerate(cols, 1):
            v = r.get(k, "")
            if k in ("shares", "cost", "fair_value", "commitment") and v:
                v = float(v)
            c = w.cell(j, i, v if v != "" else None)
            c.font = Font(name=F, size=9)
            if k in ("shares", "cost", "fair_value", "commitment"):
                c.number_format = "#,##0.##"
    w.freeze_panes = "C2"
    w.auto_filter.ref = f"A1:{chr(64 + len(cols))}{len(rows) + 1}"
    return w


by = {k: [r for r in R if r["status"] == k] for k in ("Verified", "Filing-confirmed", "Failed")}
sheet("Verified", by["Verified"], [c for c in COLS if c[0] != "reason"])
sheet("Filing-confirmed", by["Filing-confirmed"], [c for c in COLS if c[0] not in ("reason", "second_source")])
sheet("Failed", by["Failed"], [c for c in COLS if c[0] != "second_source"])
sheet("Not in evidence", U, [("form", "Form", 9), ("file_date", "Filed", 11), ("filer", "Filer", 34), ("kind", "Found in", 9), ("filing_in_evidence", "Filing used in evidence", 12), ("line", "Line naming the company, with a number", 100), ("url", "Filing link", 30)])
s["A1"] = f"{title}: evidence check"
s["A1"].font = Font(name=F, size=15, bold=True, color=navy)
s["A2"] = "Every extracted number checked against its filing by script, with no AI. The script finds problems; it does not change any number."
s["A2"].font = Font(name=F, size=10, color="555555")
hd = ["Result", "Holdings", "Facts", "Total", "What it means"]
for i, h in enumerate(hd, 1):
    c = s.cell(4, i, h); c.font = Font(name=F, bold=True, color="FFFFFF", size=10); c.fill = PatternFill("solid", fgColor=navy)
mean = {"Verified": "Printed in the filing, unit agrees, and a second source agrees (Form N-PORT or an iXBRL tag)", "Filing-confirmed": "Printed in the filing and the unit agrees; no second source", "Failed": "Not proven. Do not use until reviewed. Some rows may be right but in a layout the script cannot read"}
vals = {}
for n, (k, m) in enumerate(mean.items(), 5):
    s.cell(n, 1, k)
    s.cell(n, 2, f'=COUNTIF(\'{k}\'!$A:$A,"holding")'); s.cell(n, 3, f'=COUNTIF(\'{k}\'!$A:$A,"fact")'); s.cell(n, 4, f"=B{n}+C{n}"); s.cell(n, 5, m)
    a, b = sum(r["kind"] == "holding" for r in by[k]), sum(r["kind"] == "fact" for r in by[k])
    vals.update({f"B{n}": a, f"C{n}": b, f"D{n}": a + b})
s.cell(8, 1, "Total")
for col in "BCD":
    s[f"{col}8"] = f"=SUM({col}5:{col}7)"
    vals[f"{col}8"] = sum(vals[f"{col}{n}"] for n in (5, 6, 7))
s["A10"] = "Numbers in filings not in the evidence"; s["A10"].font = Font(name=F, size=12, bold=True, color=navy)
s["A11"] = "Lines naming the company with a number, no evidence row"
s["B11"] = "=COUNTA('Not in evidence'!$A:$A)-1"; vals["B11"] = len(U)
s["A12"] = "Of these, in filings with no evidence row at all"
s["B12"] = "=COUNTIF('Not in evidence'!$E:$E,\"no\")"; vals["B12"] = sum(r["filing_in_evidence"] == "no" for r in U)
s["A14"] = "Why rows failed"; s["A14"].font = Font(name=F, size=12, bold=True, color=navy)
why = collections.Counter(re.sub(r"\(.*", "", re.sub(r"\$[\d,.]+|[\d.]+x", "#", x.strip()))[:64].strip() for r in by["Failed"] for x in r["reason"].split(";") if x.strip())
for n, (k, v) in enumerate(why.most_common(14), 15):
    s.cell(n, 1, k); s.cell(n, 2, v)
for row in s.iter_rows(min_row=5, max_row=30):
    for c in row:
        if c.value is not None and c.font.size != 12:
            c.font = Font(name=F, size=10, bold=c.row == 8)
s.column_dimensions["A"].width = 58
for col in "BCD":
    s.column_dimensions[col].width = 11
s.column_dimensions["E"].width = 90
s.sheet_view.showGridLines = False
wb.calculation = CalcProperties(fullCalcOnLoad=True)
wb.save(dest)
tmp = dest + ".tmp"
with zipfile.ZipFile(dest) as a, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as b:
    for it in a.infolist():
        d = a.read(it.filename)
        if it.filename == "xl/worksheets/sheet1.xml":
            x = d.decode("utf-8")
            x = re.sub(r'(<c r="([A-Z]+\d+)"[^>]*><f>[^<]*</f>)<v ?/?>(?:</v>)?</c>', lambda m: f"{m[1]}<v>{vals[m[2]]}</v></c>", x)
            d = x.encode("utf-8")
        b.writestr(it, d)
shutil.move(tmp, dest)
print({k: len(v) for k, v in by.items()}, len(U), dict(why.most_common(6)))
