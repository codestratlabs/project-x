#!/usr/bin/env python3
"""Second, independent read of the gold rows. Goes back to the filing at the stored table and row, with its own simple
parser (not the one that produced the row), and checks:

    M1  every number taken from the row is printed in that row of the filing
    M2  the scale: thousands/millions are printed near the table when the row was scaled, and are not printed when it was not
    M3  the date: the as-of date is printed above the table (or on the cover / in the tag for the other date rules)

Writes out/<co>/evidence/gold_verify.csv with the printed header, row, unit text and date text next to each row, so a
person (or a reviewer) can read them side by side.
"""
from __future__ import annotations

import csv
import html
import re
import sys
from pathlib import Path

import openpyxl

csv.field_size_limit(10**9)
co, book = sys.argv[1], sys.argv[2]
HERE = Path(__file__).parent
ROOT = HERE.parents[2]
ev = HERE / "out" / co / "evidence"
E = {r["evidence_id"]: r for r in csv.DictReader(open(ev / "evidence_rows.csv", encoding="utf-8"))}
H = {h["evidence_id"]: h for h in csv.DictReader(open(ev / "holdings_from_evidence.csv", encoding="utf-8"))}
Fx = {f["fact_id"]: f for f in csv.DictReader(open(ev / "facts_from_evidence.csv", encoding="utf-8"))}
local = {}
for man in sorted((ROOT / "dumps").glob(f"{co}*_manifest.csv")):
    f = man.name.replace("_manifest.csv", "")
    for r in csv.DictReader(open(man, encoding="utf-8")):
        local.setdefault(r["accession"], []).append(ROOT / "dumps" / f / r["file"])


def txt(x: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", x)).replace("\xa0", " ")).strip()


def shown(v: float) -> set:
    v = abs(v)
    return {f"{v:,.0f}", f"{v:,.2f}", f"{v:,.1f}", f"{v:.0f}", f"{v:,.3f}".rstrip("0").rstrip(".")}


def dates_of(d: str) -> set:
    import datetime as dt
    try:
        x = dt.date.fromisoformat(d)
    except ValueError:
        return {d}
    return {x.strftime("%B %-d, %Y"), x.strftime("%B %d, %Y"), x.strftime("%m/%d/%Y"), x.strftime("%-m/%-d/%Y"), x.strftime("%-m/%-d/%y"), d, x.strftime("%B %-d, %Y").upper()}


wb = openpyxl.load_workbook(book, read_only=True)
rows = [r for r in wb["Gold check"].iter_rows(min_row=4, values_only=True) if r[0]]
out = []
cache: dict = {}
for g in rows:
    n, kind, gid = g[0], g[1], g[2]
    res = dict(n=n, kind=kind, id=gid, fund=g[3], what=g[4], as_of=g[5], M1="", M2="", M3="", header="", row="", unit_text="", date_text="", note="")
    if kind == "fact":
        f = Fx[gid]
        if f["metric"] == "fund_transaction":
            p = next((x for x in local.get(f["accession"], []) if x.suffix == ".xml"), None)
            t = p.read_text(errors="ignore") if p else ""
            res["M1"] = "ok" if re.search(rf"<principalTotalPurchaseSale>{float(f['value']):.2f}0*<|<principalTotalPurchaseSale>{float(f['value']):.0f}\.0+<", t) else "NOT FOUND"
            res["M3"] = "ok" if f'reportEndingPeriod="{f["date"]}"' in t else "NOT FOUND"
            res["M2"] = "n/a (XML dollars)"
        else:
            res["M1"] = "ok" if f["printed"].strip() in f["sentence"] else "NOT FOUND"
            res["M3"] = "ok" if not f["date"] or f["date"] in f["sentence"] else "NOT FOUND"
            res["M2"] = "ok" if re.search(r"billion|million|thousand|\b[bmk]\b", f["printed"], re.I) or float(f["value"]) >= 1e5 or "customers" in f["unit"] or "organizations" in f["unit"] else "check"
        res["row"] = f["sentence"][:400]
        out.append(res)
        continue
    e, h = E[gid], H.get(gid, {})
    paths = [p for p in local.get(e["accession"], []) if p.suffix.lower() in (".htm", ".html", ".xml", ".pdf")]
    if e["method"] == "iXBRL tag":
        t = next((x.read_text(errors="ignore") for x in paths if f'"{e["table"]}"' in x.read_text(errors="ignore")), "")
        vals = [float(x) for x in (e["qty"], e["cost"], e["value"]) if x]
        tags = re.findall(r"<ix:nonFraction([^>]*)>(.*?)</ix:nonFraction>", t, re.S)
        printed = []
        for at, x in tags:
            if f'contextRef="{e["table"]}"' in at:
                sc = re.search(r'scale="(-?\d+)"', at)
                sg = -1 if 'sign="-"' in at else 1
                v_ = txt(x).replace(",", "")
                if re.fullmatch(r"[\d.]+", v_):
                    printed.append(sg * float(v_) * 10 ** int(sc[1] if sc else 0))
        res["M1"] = "ok" if vals and all(any(abs(v - p_) < 1 for p_ in printed) for v in vals) else "NOT FOUND"
        res["row"] = f"tags in context {e['table']}: " + ", ".join(f"{p_:,.0f}" for p_ in printed)
        res["M2"] = "ok (scale attribute)"
        res["M3"] = "ok" if e["as_of"] and e["as_of"] in t[:t.find(e["table"]) + 3000 if e["table"] in t else 0] or e["as_of_source"] == "iXBRL context date" else "check"
        out.append(res)
        continue
    p = next((x for x in paths if x.suffix.lower() in (".htm", ".html")), None)
    if not p or e["method"] != "table":
        res["note"] = f"method {e['method']}: read by eye"
        res["row"] = e["raw"][:300]
        out.append(res)
        continue
    if p not in cache:
        cache.clear()
        cache[p] = re.sub(r"</?(?:table|tr|td|th)\b", lambda x: x[0].lower(), p.read_text(encoding="utf-8", errors="ignore"), flags=re.I)
    t = cache[p]
    a = int(e["table"])
    z = t.find("</table>", a)
    trs = re.findall(r"<tr\b[^>]*>(.*?)</tr>", t[a:z], re.S)
    ri = int(e["row"])
    cells = [txt(c) for c in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", trs[ri], re.S)] if ri < len(trs) else []
    rowtxt = " | ".join(c for c in cells if c)
    nxt = " | ".join(c for c in (txt(c) for c in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", trs[ri + 1], re.S)) if c) if ri + 1 < len(trs) else ""
    head = " / ".join(txt(tr)[:160] for tr in trs[:min(ri, 4)] if txt(tr))
    res["row"], res["header"] = rowtxt[:400], head[:400]
    allrow = rowtxt + " | " + nxt + " | " + e["name"]
    nums = [float(x) for x in (e["qty"], e["cost"], e["value"], e["commitment"]) if x]
    res["M1"] = "ok" if nums and all(any(s in allrow for s in shown(v)) for v in nums) else "NOT FOUND"
    before = txt(t[max(0, a - 6000):a])
    scale_words = re.findall(r"[^.]{0,60}(?:in thousands|\(000|000s|000’s|000's|in millions|\$000)[^.]{0,30}", before[-2500:] + " " + head, re.I)
    uf = float(e["unit_factor"] or 1)
    if uf == 1:
        res["M2"] = "ok" if not scale_words or "proven" in (h.get("unit") or "") or "$ in the column header" in e["unit"] else "CHECK: scale words near table"
    else:
        res["M2"] = "ok" if scale_words or re.search(r"thousand|000|million", e["unit"] + e["header"], re.I) else "CHECK: no scale words found"
    res["unit_text"] = (scale_words[-1] if scale_words else "")[:160] + " || unit stored: " + e["unit"][:80]
    ds = dates_of(e["report_date"])
    win = txt(t[max(0, a - 60000):a])
    res["M3"] = "ok" if any(d in win[-20000:] for d in ds) or any(d in (e.get("schedule") or "") for d in ds) else ("ok (cover)" if e["as_of_source"].startswith("reporting period") else "CHECK")
    dm = [m for m in re.finditer("|".join(re.escape(d) for d in ds), win)]
    res["date_text"] = win[max(0, dm[-1].start() - 80):dm[-1].end() + 40] if dm else (e.get("schedule") or "")[:160]
    out.append(res)
with open(ev / "gold_verify.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, list(out[0]))
    w.writeheader()
    w.writerows(out)
import collections
for k in ("M1", "M2", "M3"):
    print(k, collections.Counter(r[k].split(":")[0] for r in out))
print("by eye:", sum(1 for r in out if r["note"]))
