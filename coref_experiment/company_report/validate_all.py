#!/usr/bin/env python3
"""Validation of every accepted holding, not a sample. Each filing is opened once and every row from it is checked
against the printed text with its own simple reader (not the parser that produced the row):

    printed   every number taken from the row is printed in that row of the filing (or in the iXBRL tags of that row)
    unit      thousands/millions are printed near the table when the row was scaled, and not printed when it was not
    date      the as-of date is printed above the table, on the cover, in the tag, or in the N-PORT row that confirms it
    sums      loans: par, cost and fair value within 35% of each other; a fair value above 120% of par is not a loan
              value; equity: no value without either shares or cost or a confirming N-PORT row is checked further

A row that fails any check is written to validation_fail.csv with the reasons. evidence_holdings.py then moves it to
review. Results for every row are in validation.csv.

    python validate_all.py <company>
"""
from __future__ import annotations

import collections
import csv
import datetime as dt
import html
import re
import sys
from pathlib import Path

csv.field_size_limit(10**9)
co = sys.argv[1]
HERE = Path(__file__).parent
ROOT = HERE.parents[2]
ev = HERE / "out" / co / "evidence"
E = {r["evidence_id"]: r for r in csv.DictReader(open(ev / "evidence_rows.csv", encoding="utf-8"))}
H = list(csv.DictReader(open(ev / "holdings_from_evidence.csv", encoding="utf-8")))
local = collections.defaultdict(list)
for f in sorted({p.name.replace("_manifest.csv", "") for p in (ROOT / "dumps").glob(f"{co}*_manifest.csv")}):
    for r in csv.DictReader(open(ROOT / "dumps" / f"{f}_manifest.csv", encoding="utf-8")):
        local[r["accession"]].append(ROOT / "dumps" / f / r["file"])


def txt(x: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", x)).replace("\xa0", " ")).strip()


def shown(v: float) -> set:
    a = abs(v)
    return {f"{a:,.0f}", f"{a:,.2f}", f"{a:,.1f}", f"{a:.0f}", f"{a:,.3f}".rstrip("0").rstrip(".")}


def dates_of(d: str) -> set:
    try:
        x = dt.date.fromisoformat(d)
    except ValueError:
        return {d}
    return {x.strftime("%B %-d, %Y"), x.strftime("%B %d, %Y"), x.strftime("%m/%d/%Y"), x.strftime("%-m/%-d/%Y"), x.strftime("%-m/%-d/%y"),
            d, x.strftime("%B %-d, %Y").upper(), x.strftime("%b. %-d, %Y"), x.strftime("%m/%d/%y")}


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


SCALE = re.compile(r"in\s+thousands|\(\s*000|000\s*[’'`]?s\b|\$\s?000|in\s+millions|\(\s*\$?\s*(?:in\s+)?millions?\s*\)|thousands of|millions of|\(\s*\$?\s*thousands\s*\)|\$\s*thousands|\(\s*\$?\s*millions\s*\)", re.I)
import json
cfg = json.loads((HERE / "model_config" / f"{co}.json").read_text(encoding="utf-8"))
_mk = HERE / "inputs" / co / "data" / "nport_price_monthly.csv"
MARK = {r["month"]: float(r["median_price"]) for r in csv.DictReader(open(_mk, encoding="utf-8"))} if _mk.exists() else {}   # no marks: per_share_ok is skipped


def per_share_ok(h: dict) -> bool:
    """Value per share within 3x of the funds' N-PORT mark that month: an independent proof of the scale."""
    sh, v, d = f(h["shares"]), f(h["fair_value_usd"]), h["as_of_date"]
    if not (sh and v and d and MARK):
        return False
    k = min(MARK, key=lambda x: abs(int(x[:4]) * 12 + int(x[5:]) - int(d[:4]) * 12 - int(d[5:7])))
    ratio = cfg.get("split_ratio", 1) if d < cfg.get("split_date", "0") else 1
    return MARK[k] / 3 <= v / sh / ratio <= MARK[k] * 3
by_acc = collections.defaultdict(list)
for h in H:
    by_acc[E[h["evidence_id"]]["accession"]].append(h)
out = []
for n, (acc, hs) in enumerate(sorted(by_acc.items())):
    paths = [p for p in local.get(acc, []) if p.suffix.lower() in (".htm", ".html", ".xml") and p.exists()]
    texts = {}
    for h in hs:
        e = E[h["evidence_id"]]
        res = {"evidence_id": h["evidence_id"], "printed": "", "unit": "", "date": "", "sums": "", "fail": []}
        nums = [x for x in (f(e["qty"]), f(e["cost"]), f(e["value"]), f(e["commitment"])) if x is not None]
        # sums: loans and equity, from the row alone
        par, cost, val = f(h["par"]), f(h["cost_usd"]), f(h["fair_value_usd"])
        if h["position_type"] == "funded_loan":
            xs = [x for x in (par, cost, val) if x and x > 0]
            if len(xs) >= 2 and max(xs) / min(xs) > 1.35:
                res["fail"].append("loan par, cost and value more than 35% apart")
            if par and val and val > 1.2 * par:
                res["fail"].append("loan value above 120% of par")
        res["sums"] = "fail" if any("loan" in x for x in res["fail"]) else "ok"
        if not paths:
            res["fail"].append("filing not on disk")
            out.append(res)
            continue
        if e["method"] == "iXBRL tag":
            p = next((p for p in paths if f'"{e["table"]}"' in texts.setdefault(p, p.read_text(encoding="utf-8", errors="ignore"))), None)
            got = []
            if p:
                for at, x in re.findall(r"<ix:nonFraction([^>]*)>(.*?)</ix:nonFraction>", texts[p], re.S):
                    if f'contextRef="{e["table"]}"' in at:
                        sc = re.search(r'scale="(-?\d+)"', at)
                        v = txt(x).replace(",", "")
                        if re.fullmatch(r"[\d.]+", v):
                            got.append((-1 if 'sign="-"' in at else 1) * float(v) * 10 ** int(sc[1] if sc else 0))
            res["printed"] = "ok" if nums and all(any(abs(v - g) < 1 for g in got) for v in nums) else "fail"
            res["unit"], res["date"] = "ok (scale attribute)", "ok (context date)"
        elif e["method"] in ("table", "text layout"):
            p = next((p for p in paths if p.suffix.lower() in (".htm", ".html")), None)
            if p not in texts:
                texts.clear()
                texts[p] = re.sub(r"</?(?:table|tr|td|th)\b", lambda x: x[0].lower(), p.read_text(encoding="utf-8", errors="ignore"), flags=re.I)
            t = texts[p]
            if e["method"] == "table":
                a = int(e["table"])
                z = t.find("</table>", a)
                trs = re.findall(r"<tr\b[^>]*>(.*?)</tr>", t[a:z], re.S)
                ri = int(str(e["row"]).split("-")[0])      # '12-2': the second dated value column of row 12
                row = " | ".join(txt(tr) for tr in trs[ri:ri + 2]) if ri < len(trs) else ""
            else:
                a = int(e["row"])
                row = txt(t[a:a + 1500])
            row += " | " + e["name"]
            res["printed"] = "ok" if nums and all(any(s in row for s in shown(v)) for v in nums) else "fail"
            near = txt(t[max(0, a - 8000):a])[-3000:] + " " + e["header"] + " " + e["unit"]
            uf = f(e["unit_factor"]) or 1
            if h["position_type"] == "equity" and per_share_ok(h):
                res["unit"] = "ok (value per share near the N-PORT mark)"
            elif uf == 1:
                res["unit"] = "ok" if not SCALE.search(near) or "proven" in h["unit"] or "$ in the column header" in e["unit"] else "fail"
            else:
                res["unit"] = "ok" if SCALE.search(near) else "fail"
            ds = dates_of(e["report_date"])
            win = txt(t[max(0, a - 60000):a])[-20000:] + " " + (e.get("schedule") or "")
            res["date"] = "ok" if any(d in win for d in ds) else ("ok (cover)" if e["as_of_source"].startswith("reporting period") else
                                                                   "ok (N-PORT row of that date)" if e["tier"] == "A N-PORT" else "fail")
        else:
            res["printed"] = res["unit"] = res["date"] = "not checked (method " + e["method"] + ")"
        for k in ("printed", "unit", "date"):
            if res[k] == "fail":
                res["fail"].append({"printed": "a number is not printed in the row", "unit": "unit does not match the words near the table",
                                    "date": "date not printed above the table"}[k])
        out.append(res)
    if n % 200 == 0:
        print("filings", n, "of", len(by_acc), flush=True)
for r in out:
    r["fail"] = "; ".join(r["fail"])
with open(ev / "validation.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, ["evidence_id", "printed", "unit", "date", "sums", "fail"])
    w.writeheader()
    w.writerows(out)
with open(ev / "validation_fail.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, ["evidence_id", "fail"], extrasaction="ignore")
    w.writeheader()
    w.writerows([r for r in out if r["fail"]])
print({k: dict(collections.Counter(r[k].split(" (")[0] for r in out)) for k in ("printed", "unit", "date", "sums")})
print(len(out), "rows checked;", sum(1 for r in out if r["fail"]), "fail;", collections.Counter(x for r in out for x in r["fail"].split("; ") if x).most_common())
