#!/usr/bin/env python3
"""Compare two findings workbooks (pipeline/findings_excel.py) sheet by sheet: cell values and hyperlink targets.

    python deploy/e2e/compare_workbooks.py EXPECTED.xlsx GOT.xlsx [--json report.json] [--examples 25]

How each sheet is compared:

- All evidence, Form D offerings, DOL 401(k), Holdings (filings), N-PORT holdings: cell for cell, the value and the
  hyperlink (target and location) of every cell. When cells differ, the data rows are also compared as rows
  (ignoring the "#" column), to tell rows that only changed place from rows that changed.
- Indirect mentions: the data rows (language-model decisions), as rows.
- About: cell for cell, except that "<n> places found; <m> sit in a paragraph ..." in the Indirect mentions row is
  compared with the numbers masked: the counts depend on the model run (they are reported, not counted as a
  difference).

Exit code 0 when nothing differs apart from those model counts, 1 otherwise. openpyxl reads both files; nothing is
written except the JSON report.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

CELLS = ("All evidence", "Form D offerings", "DOL 401(k)", "Holdings (filings)", "N-PORT holdings")
ROWS = ("Indirect mentions",)
ABOUT = "About"
MODEL_COUNT = re.compile(r"\d+ places found; \d+ sit in a paragraph")
MASKED = "<n> places found; <m> sit in a paragraph"


def short(value, width: int = 140) -> str:
    text = repr(value)
    return text if len(text) <= width else text[: width - 3] + "..."


def link_of(cell) -> str | None:
    """A cell's hyperlink as one string: target, then '#' and the location when there is one."""
    h = cell.hyperlink
    if h is None:
        return None
    return (h.target or "") + (f"#{h.location}" if h.location else "")


def grid(ws) -> dict:
    """{(row, column): (value, link)} for every cell that has a value or a link."""
    out = {}
    for row in ws.iter_rows():
        for c in row:
            link = link_of(c)
            if c.value is not None or link:
                out[(c.row, c.column)] = (c.value, link)
    return out


def header(ws) -> list:
    return [c.value for c in next(ws.iter_rows(min_row=1, max_row=1), [])]


def data_rows(ws) -> list[tuple]:
    """Each data row as a tuple of (value, link) pairs without the '#' column (column A holds the row number)."""
    head = header(ws)
    skip = {i for i, h in enumerate(head) if h == "#"}
    rows = []
    for row in ws.iter_rows(min_row=2):
        cells = [(c.value, link_of(c)) for i, c in enumerate(row) if i not in skip]
        if any(v is not None or l for v, l in cells):
            rows.append(tuple(cells))
    return rows


def coord(row: int, col: int) -> str:
    from openpyxl.utils import get_column_letter
    return f"{get_column_letter(col)}{row}"


def compare_cells(we, wg, examples: int, mask=None) -> dict:
    """Cell-for-cell comparison. mask(value) -> value is applied to both sides first (About only)."""
    from openpyxl.utils import get_column_letter

    ge, gg = grid(we), grid(wg)
    head = [] if mask else header(we)                  # About's first row is a title, not a header
    diffs, masked, by_column = [], [], collections.Counter()
    for rc in sorted(set(ge) | set(gg)):
        ve, le = ge.get(rc, (None, None))
        vg, lg = gg.get(rc, (None, None))
        name = head[rc[1] - 1] if rc[1] - 1 < len(head) else f"column {get_column_letter(rc[1])}"
        if ve != vg:
            if mask and mask(ve) == mask(vg):
                masked.append({"cell": coord(*rc), "expected": ve, "got": vg})
            else:
                diffs.append({"cell": coord(*rc), "column": name, "what": "value", "expected": ve, "got": vg})
                by_column[f"{name} (value)"] += 1
        if le != lg:
            diffs.append({"cell": coord(*rc), "column": name, "what": "link", "expected": le, "got": lg})
            by_column[f"{name} (link)"] += 1
    out = {"rows": [we.max_row - 1, wg.max_row - 1], "columns": [we.max_column, wg.max_column],
           "header_same": header(we) == header(wg), "differences": len(diffs), "by_column": dict(by_column),
           "examples": diffs[:examples]}
    if mask:
        out["model_counts"] = masked
    if diffs and not mask:
        out.update(compare_rows(we, wg, examples, keep_examples=False))
    return out


def compare_rows(we, wg, examples: int, keep_examples: bool = True) -> dict:
    """The data rows of both sheets as multisets (the '#' column left out): rows only in one of them."""
    re_, rg = data_rows(we), data_rows(wg)
    ce, cg = collections.Counter(re_), collections.Counter(rg)
    only_e, only_g = list((ce - cg).elements()), list((cg - ce).elements())
    head = [h for h in header(we) if h != "#"]

    def show(row):
        return {h: (v if l is None else {"value": v, "link": l}) for h, (v, l) in zip(head, row) if v is not None or l}

    out = {"data_rows": [len(re_), len(rg)], "rows_only_expected": len(only_e), "rows_only_got": len(only_g),
           "same_rows_other_order": not only_e and not only_g and re_ != rg}
    if keep_examples or only_e or only_g:
        out["rows_only_expected_examples"] = [show(r) for r in only_e[:examples]]
        out["rows_only_got_examples"] = [show(r) for r in only_g[:examples]]
    if not keep_examples:
        out["differences_as_rows"] = True
    else:
        out["differences"] = len(only_e) + len(only_g) + (1 if out["same_rows_other_order"] else 0)
    return out


def compare(expected: Path, got: Path, examples: int = 25) -> dict:
    from openpyxl import load_workbook

    we, wg = load_workbook(expected), load_workbook(got)
    report = {"expected": str(expected), "got": str(got), "sheets_expected": we.sheetnames, "sheets_got": wg.sheetnames,
              "sheet_order_same": we.sheetnames == wg.sheetnames, "sheets": {}}
    for name in (*CELLS, *ROWS, ABOUT):
        if name not in we.sheetnames or name not in wg.sheetnames:
            report["sheets"][name] = {"mode": "missing", "in_expected": name in we.sheetnames,
                                      "in_got": name in wg.sheetnames, "differences": 1}
            continue
        if name in CELLS:
            res = {"mode": "cells", **compare_cells(we[name], wg[name], examples)}
        elif name in ROWS:
            res = {"mode": "rows", "header_same": header(we[name]) == header(wg[name]),
                   **compare_rows(we[name], wg[name], examples)}
            res["differences"] += 0 if res["header_same"] else 1
        else:
            res = {"mode": "about", **compare_cells(we[name], wg[name], examples,
                                                    mask=lambda v: MODEL_COUNT.sub(MASKED, v) if isinstance(v, str) else v)}
        report["sheets"][name] = res
    other = sorted((set(we.sheetnames) | set(wg.sheetnames)) - set(report["sheets"]))
    report["other_sheets"] = other
    report["identical"] = (report["sheet_order_same"] and not other
                           and all(r.get("differences", 0) == 0 for r in report["sheets"].values()))
    return report


def render(report: dict) -> str:
    lines = [f"expected: {report['expected']}", f"got:      {report['got']}",
             f"sheets: {'same names and order' if report['sheet_order_same'] else 'DIFFER'}: {report['sheets_got']}"
             + ("" if report["sheet_order_same"] else f" (expected {report['sheets_expected']})")]
    if report["other_sheets"]:
        lines.append(f"sheets not compared: {report['other_sheets']}")
    for name, r in report["sheets"].items():
        if r["mode"] == "missing":
            lines.append(f"- {name}: MISSING (in expected: {r['in_expected']}, in got: {r['in_got']})")
            continue
        n = r.get("differences", 0)
        if r["mode"] in ("cells", "about"):
            dims = f"{r['rows'][1]} data rows x {r['columns'][1]} columns" + (
                "" if r["rows"][0] == r["rows"][1] and r["columns"][0] == r["columns"][1]
                else f" (expected {r['rows'][0]} x {r['columns'][0]})")
            what = "identical cell for cell (values and links)" if not n else f"{n} cell difference(s)"
            lines.append(f"- {name}: {what}; {dims}")
            if r["mode"] == "about":
                for m in r.get("model_counts", []):
                    lines.append(f"    model-run count, not a difference: {m['cell']}: "
                                 f"{MODEL_COUNT.search(str(m['expected'])).group(0) if MODEL_COUNT.search(str(m['expected'])) else short(m['expected'])}"
                                 f" -> {MODEL_COUNT.search(str(m['got'])).group(0) if MODEL_COUNT.search(str(m['got'])) else short(m['got'])}")
            if n:
                lines.append(f"    by column: {r['by_column']}")
                if r.get("differences_as_rows"):
                    lines.append(f"    as rows: {r['rows_only_expected']} row(s) only in expected, {r['rows_only_got']} only in got"
                                 + ("; the same rows in another order" if r.get("same_rows_other_order") else ""))
                for d in r["examples"]:
                    lines.append(f"    {d['cell']} [{d['column']}] {d['what']}: expected {short(d['expected'])} | got {short(d['got'])}")
                for side in ("expected", "got"):
                    for row in r.get(f"rows_only_{side}_examples", []):
                        lines.append(f"    row only in {side}: {short(row, 600)}")
        else:
            lines.append(f"- {name}: {r['data_rows'][1]} data rows (expected {r['data_rows'][0]}); "
                         f"{r['rows_only_expected']} only in expected, {r['rows_only_got']} only in got"
                         + ("" if r["header_same"] else "; HEADER DIFFERS")
                         + ("; same rows in another order" if r.get("same_rows_other_order") else ""))
            for side in ("expected", "got"):
                for row in r.get(f"rows_only_{side}_examples", []):
                    lines.append(f"    row only in {side}: {short(row, 600)}")
    lines.append("RESULT: " + ("identical (apart from model-run counts in About)" if report["identical"] else "differences found"))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare two findings workbooks: values and hyperlink targets.")
    ap.add_argument("expected", type=Path)
    ap.add_argument("got", type=Path)
    ap.add_argument("--json", type=Path, help="write the full report here")
    ap.add_argument("--examples", type=int, default=25, help="differences listed per sheet (default 25)")
    a = ap.parse_args()
    report = compare(a.expected, a.got, a.examples)
    print(render(report))
    if a.json:
        a.json.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    return 0 if report["identical"] else 1


if __name__ == "__main__":
    sys.exit(main())
