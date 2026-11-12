#!/usr/bin/env python3
"""Holdings for the report from the evidence rows only, by rule; no model. Each holding keeps the evidence id of the row it
came from, so every number in the report can be traced to a filing, table and row.

    python evidence_holdings.py <company>

Only rows graded A (iXBRL tag, or equal to a Form N-PORT row of the same date) or B (header and unit printed) with a date
are used. Rows in review are not.

Fund (holder):   the N-PORT series when exactly one N-PORT row matched; else the fund named in the heading of the
                 holdings table; else the filer.
Position type:   unfunded commitment, loan, SPV interest or equity, from the printed column and security name.
Security:        series or class as printed ("Series H", "Common").
Acquisition date: the printed acquisition-date column, or "Acquired mm/dd/yy" in the security name.
"""
from __future__ import annotations

import collections
import csv
import json
import datetime as dt
import re
import sys
from pathlib import Path

csv.field_size_limit(10**9)
co = sys.argv[1]
HERE = Path(__file__).parent
ev = HERE / "out" / co / "evidence"
R = [r for r in csv.DictReader(open(ev / "evidence_rows.csv", encoding="utf-8")) if r["tier"] in ("A tag", "A N-PORT", "B stated")]
NAME_RX = json.loads((HERE / "model_config" / f"{co}.json").read_text(encoding="utf-8"))["company"]["name_regex"]
FAILED = {}
if "--use-validation" in sys.argv and (ev / "validation_fail.csv").exists():      # rows that failed validate_all.py go to review
    FAILED = {x["evidence_id"]: x["fail"] for x in csv.DictReader(open(ev / "validation_fail.csv", encoding="utf-8"))}
    with open(ev / "holdings_failed_validation.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["evidence_id", "why", "form", "filer", "as_of", "name", "raw", "url"])
        w.writerows([[r["evidence_id"], FAILED[r["evidence_id"]], r["form"], r["filer"], r["report_date"], r["name"], r["raw"], r["url"]] for r in R if r["evidence_id"] in FAILED])
    R = [r for r in R if r["evidence_id"] not in FAILED]

LOAN = re.compile(r"term\s+loan|delayed\s+draw|revolv|first\s+lien|1st\s+lien|second\s+lien|senior\s+secured|\bloan\b|\bnotes?\b|\bDDTL\b|\bTL\b|SOFR|S\+", re.I)
SPV = re.compile(r"economic\s+exposure|\bL\.?L\.?C\.?\b|\bL\.?P\.?\b|\bSPV\b|feeder|partnership|class\s+\d+\s+interest|units?\s+of", re.I)
SERIES = re.compile(r"\b(?:Series|Ser\.)\s*([A-Z]{1,2}(?:-\d)?\d?)\b|\b(Common|Preferred|Class\s+[A-Z])\b", re.I)
FUNDNAME = re.compile(r"((?:[A-Z0-9][\w®&.'’-]*\s+|\([A-Z]{1,4}\)\s+){0,9}(?:Fund|Funds|Portfolio|Trust|Corp\.?|Corporation|Company|Co\.?|Inc\.?|LLC|L\.P\.)(?:,?\s+(?:I{1,3}|IV|VI{0,3}|\d))?)(?=\W|$)")
FUNDWORD = re.compile(r"\b(fund|portfolio|trust|series|income|growth|equity|opportunit|capital|technology|value|index|strategies)\b", re.I)


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def acq_date(r: dict) -> str:
    for s in (r.get("date") or "", r["name"]):
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
        if m:
            y = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
            try:
                return dt.date(y, int(m[1]), int(m[2])).isoformat()
            except ValueError:
                return ""
    return ""


SERIES_OF = collections.defaultdict(set)          # registrant CIK -> its fund series names, from Form N-PORT
_np = HERE.parents[2] / "dumps" / f"{co}_nport" / "nport_holdings.csv"
for x in (csv.DictReader(open(_np, encoding="utf-8")) if _np.exists() else []):   # no N-PORT file: holders stay the filer
    SERIES_OF[x["cik"].lstrip("0")].add(x["series"] or x["registrant"])


def key_words(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower().replace("®", "")) if w not in ("fund", "the", "inc", "portfolio", "corp", "co", "of")}


def holder(r: dict) -> tuple[str, str]:
    """The fund that holds the row. A name read from the table heading is used only when it is one of the filer's own
    fund series in Form N-PORT; otherwise the filer is named."""
    if r.get("nport_series"):
        return r["nport_series"], "N-PORT series of the matching row"
    filer = re.sub(r"\s*\(CIK \d+\)\s*", "", r["filer"]).strip()
    cik = re.search(r"CIK (\d+)", r["filer"])
    h = r.get("schedule") or ""
    m = re.search(r"(?:schedules?|statements?|portfolio|summary)\s+of\s+investments|investment\s+portfolio", h, re.I)
    if m and cik:
        before = re.sub(r"\b(?:Table\s+)?of\s+Contents\b|\bConsolidated\b|\bCondensed\b", " ", h[:m.start()], flags=re.I)[-120:]
        names = list(FUNDNAME.finditer(before))
        if names:
            n = key_words(names[-1][1])
            hits = [x for x in SERIES_OF.get(cik[1].lstrip("0"), ()) if n and key_words(x) == n]
            if len(hits) == 1:
                return hits[0], "fund named in the table heading, same as a Form N-PORT series of the filer"
    return filer, "filer (the fund inside the filing is not named by rule)"


def ptype(r: dict) -> str:
    t = f"{r['name']} {r['raw']} {r['header']}"
    v, c = f(r.get("value_usd")), f(r.get("commitment_usd"))
    if c and not v:
        return "unfunded_commitment"
    nm = r["name"] + " " + (r["raw"].split("|")[1] if "|" in r["raw"] else "")
    if re.search(r"invested\s+in|economic\s+exposure|exposure\s+to|through\s+(?:an?\s+)?(?:SPV|special purpose)|\bSPV\b|co[- ]?invest", nm, re.I) or (
            SPV.search(r["name"]) and not re.search(rf"(?:{NAME_RX})\W{{0,3}}(?:inc|corp|llc|ltd)\b", r["name"], re.I)):
        return "spv_interest"                          # an interest in a vehicle that holds the company, not its shares
    if r.get("nport_asset_cat") in ("LON", "DBT"):
        return "funded_loan"
    if r.get("nport_asset_cat") in ("EC", "EP"):
        return "equity"
    if r["method"] == "iXBRL tag":                # the tag names say what the position is
        if "Unfunded" in r["raw"] and not v:
            return "unfunded_commitment"
        if re.search(r"InterestRate|BasisSpread|PrincipalAmount|Maturity", r["raw"]):
            return "funded_loan"
    loan_words = LOAN.search(r["name"] + " " + r["raw"])
    if loan_words or (re.search(r"principal|\bpar\b", r["header"], re.I) and not re.search(r"shares", r["header"], re.I)):
        return "unfunded_commitment" if re.search(r"unfunded|undrawn", t, re.I) and not v else "funded_loan"
    return "equity"


def security(r: dict) -> str:
    m = SERIES.search(r["name"]) or SERIES.search(r["raw"].split("|")[1] if "|" in r["raw"] else "")
    if not m:
        return ""
    return f"Series {m[1].upper()}" if m[1] else m[2].title()


out, seen = [], {}
for r in R:
    pt = ptype(r)
    q, c, v, cm = (f(r.get(k)) for k in ("qty_usd", "cost_usd", "value_usd", "commitment_usd"))
    hd, hs = holder(r)
    shares = q if pt == "equity" else None
    if not any(x for x in (q, c, v, cm)):
        continue                                       # no amount in the row: not a position
    row = dict(holder=hd, holder_source=hs, vehicle="direct" if pt != "spv_interest" else r["name"][:90], position_type=pt, security=security(r),
               shares=shares, par=q if pt in ("funded_loan", "unfunded_commitment") else None, cost_usd=c, fair_value_usd=v, commitment_usd=cm,
               price_per_share=v / shares if v and shares else None, cost_per_share=c / shares if c and shares else None,
               acquisition_date=acq_date(r), as_of_date=r["report_date"], as_of_source=r["as_of_source"], form=r["form"],
               filer=r["filer"], filing_date=r["file_date"], sec_url=r["url"], evidence_id=r["evidence_id"], tier=r["tier"],
               header=r["header"], unit=r["unit"], raw=r["raw"], table=r["table"], row=r["row"], sources=1, other_evidence_ids="",
               shared_with="", value_scale=1, shares_scale=1, pct_net_assets="", page="", evidence=r["raw"][:200],
               confidence=100, nport_check=r["tier"], repair="")
    k = (re.sub(r"\W+", " ", hd.lower()).strip(), row["security"], pt, row["as_of_date"], round(v or 0), round(q or 0), round(c or 0))
    if k in seen:                                   # the same position in another filing (annual report and prospectus ...)
        o = seen[k]
        o["sources"] += 1
        o["other_evidence_ids"] = (o["other_evidence_ids"] + " " + r["evidence_id"]).strip()
        if r["file_date"] > o["filing_date"] or (r["tier"] < o["tier"]):
            keep = {x: o[x] for x in ("sources", "other_evidence_ids")}
            o.update(row, **keep)
        continue
    seen[k] = row
    out.append(row)
# two filings that give the same position (fund, type, security, date, acquisition date, loan terms) different numbers
# contradict each other: both go to review
def desc(h: dict) -> str:
    if h["position_type"] == "equity":            # an interest held through a named vehicle is its own position
        v = re.search(r"([A-Z][\w&.'’ -]{2,60}?(?:LLC|L\.?P\.?|Fund\s+[IVX]+|Fund|Holdings|SPV)\b)[^|]{0,40}\(?invested in", h["raw"])
        return v[1].lower() if v else ""
    cells = [c.strip() for c in h["raw"].split("|")[1:]]
    return " ".join(c for c in cells if not re.fullmatch(r"[\d,.()$%\s—–-]*", c))[:120].lower()


grp = collections.defaultdict(list)
for h in out:
    if h["holder_source"].startswith("filer"):               # several funds can share one filer: no fund to compare against
        continue
    grp[(h["holder"], (re.search(r"CIK (\d+)", h["filer"]) or [None, h["filer"]])[1], h["position_type"], h["security"], h["as_of_date"], h["acquisition_date"], desc(h))].append(h)
conflict = []
def differ(hs: list) -> bool:
    """Two rows disagree only on a field both of them print (a valuation table that gives the value without shares agrees
    with the schedule row that gives both)."""
    for k_ in ("fair_value_usd", "shares", "par", "cost_usd"):
        vals = [h[k_] for h in hs if h[k_] not in (None, "")]
        if len(vals) > 1 and max(vals) - min(vals) > max(1.0, abs(max(vals)) * 0.0005) + 1000 * (k_ != "shares"):
            return True
    return False


for k, hs in grp.items():
    if len(hs) > 1 and not differ(hs):              # the same position told twice: keep the fullest row
        best = max(hs, key=lambda h: sum(h[x] not in (None, "") for x in ("shares", "par", "cost_usd", "fair_value_usd")))
        for h in hs:
            if h is not best:
                best["sources"] += h["sources"]
                best["other_evidence_ids"] = (best["other_evidence_ids"] + " " + h["evidence_id"]).strip()
                h["why"] = "merged"
        continue
    if len(hs) > 1:
        for h in hs:
            h["why"] = f"{len(hs)} filings give this position different numbers"
        conflict += hs
out = [h for h in out if not h.get("why")]
conflict = [h for h in conflict if h.get("why") != "merged"]
with open(ev / "holdings_conflicts.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, list(conflict[0]) if conflict else ["why"])
    w.writeheader()
    w.writerows(conflict)
print(len(conflict), "rows in", len({id(g_) for g_ in grp.values() if any(h.get("why", "").endswith("different numbers") for h in g_)}), "contradicting groups moved to review")
out.sort(key=lambda h: (h["holder"].lower(), h["as_of_date"], h["filing_date"]))
head = [k for k in out[0] if k != "why"] if out else []
with open(ev / "holdings_from_evidence.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, head)
    w.writeheader()
    w.writerows(out)
print(len(R), "evidence rows ->", len(out), "holdings;", dict(collections.Counter(h["position_type"] for h in out)),
      dict(collections.Counter(h["holder_source"] for h in out)))
