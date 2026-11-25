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
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # pipeline/: paths.py, pages.py
import paths as px  # noqa: E402

csv.field_size_limit(10**9)
co = sys.argv[1]
HERE = Path(__file__).parent
ev = px.OUT / co / "evidence"
R = [r for r in csv.DictReader(open(ev / "evidence_rows.csv", encoding="utf-8")) if r["tier"] in ("A tag", "A N-PORT", "B stated")]
NAME_RX = json.loads(px.config_path(co).read_text(encoding="utf-8"))["company"]["name_regex"]
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


INLINE_COST = re.compile(r"\bcost\s*:?\s*(?:\|\s*)?\$\s*(?:\|\s*)?(\d{1,3}(?:,\d{3})+|\d+)(?![\d,])", re.I)   # boxes of a laid-out page: "cost | $88,431)"


def joined_cost(raw: str) -> str:
    """A laid-out page can split one printed cost over two boxes: "cost | $3,030,0 | 10 | )". The two pieces are one
    number only when together they make a well-formed number and the first piece alone does not."""
    well = re.compile(r"\d{1,3}(?:,\d{3})+|\d{1,3}")
    def fix(m):
        if well.fullmatch(m[1]) or not well.fullmatch(m[1] + m[2]):
            return m[0]
        return "$" + m[1] + m[2] + " )"
    return re.sub(r"\$\s*(\d[\d,]*)\s*\|\s*(\d{1,3})\s*\|?\s*\)", fix, raw)


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


sys.path.insert(0, str(HERE))
PATHS = {x["sec_url"]: px.DUMPS / m.name.replace("_manifest.csv", "") / x["file"] for m in px.DUMPS.glob(f"{co}*_manifest.csv")
         for x in csv.DictReader(open(m, encoding="utf-8"))}
SERIES_OF = collections.defaultdict(set)          # registrant CIK -> its fund series names, from Form N-PORT
NP_CIK, REG_NAME = {}, {}                          # N-PORT accession -> registrant CIK; CIK -> registrant name
_np = px.DUMPS / f"{co}_nport" / "nport_holdings.csv"
for x in (csv.DictReader(open(_np, encoding="utf-8")) if _np.exists() else []):   # no N-PORT file: holders stay the filer
    SERIES_OF[x["cik"].lstrip("0")].add(x["series"] or x["registrant"])
    NP_CIK[x["accession"]] = x["cik"].lstrip("0")
    REG_NAME.setdefault(x["cik"].lstrip("0"), x["registrant"])


def key_words(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower().replace("®", "")) if w not in ("fund", "the", "inc", "portfolio", "corp", "co", "of")}


def fund_key(s: str) -> str:
    """One spelling per fund: 'BlackRock Science & Technology Trust  (BST)' and 'BlackRock Science and Technology Trust' are one key."""
    s = re.sub(r"\(CIK \d+\)|\([A-Z]{2,6}\)", " ", s).lower().replace("&", " and ").replace("®", " ")
    return " ".join(w for w in re.findall(r"[a-z0-9]+", s) if w not in ("inc", "corp", "corporation", "llc", "lp", "l", "p", "the", "co"))


REG_OF = {}                                        # series key -> its registrant's key (the umbrella trust that files for it)
for x in (csv.DictReader(open(_np, encoding="utf-8")) if _np.exists() else []):
    if x["series"] and x["registrant"]:
        REG_OF.setdefault(fund_key(x["series"]), set()).add(fund_key(x["registrant"]))
KNOWN = {}                                         # fund key -> (name, CIK): Form N-PORT funds that hold the company, and every filer
for x in (csv.DictReader(open(_np, encoding="utf-8")) if _np.exists() else []):
    one = len(SERIES_OF[x["cik"].lstrip("0")]) == 1          # an umbrella trust's own name is not one fund
    for nm in ([x["series"]] if x["series"] else []) + ([x["registrant"]] if one or not x["series"] else []):
        if nm and nm.upper() != "N/A":
            KNOWN.setdefault(fund_key(nm), (nm, x["cik"].lstrip("0")))
for x in R:
    c_ = re.search(r"CIK (\d+)", x["filer"])
    KNOWN.setdefault(fund_key(x["filer"]), (re.sub(r"\s*\(CIK \d+\)\s*", "", x["filer"]).strip(), c_[1].lstrip("0") if c_ else ""))
KNOWN.pop("", None)
SHARED = collections.defaultdict(set)              # fund key -> {(name, CIK)} when one key names funds of several CIKs
for x in (csv.DictReader(open(_np, encoding="utf-8")) if _np.exists() else []):
    for nm in (x["series"], x["registrant"]):
        if nm and nm.upper() != "N/A":
            SHARED[fund_key(nm)].add((nm, x["cik"].lstrip("0")))
for x in R:
    c_ = re.search(r"CIK (\d+)", x["filer"])
    if c_:
        SHARED[fund_key(x["filer"])].add((re.sub(r"\s*\(CIK \d+\)\s*", "", x["filer"]).strip(), c_[1].lstrip("0")))
SHARED = {k: v for k, v in SHARED.items() if len({c for _, c in v}) > 1}


def fid(h: dict) -> str:
    """One fund: its name key and its registrant's CIK ('T. Rowe Price Mid-Cap Growth Portfolio' of T. Rowe Price Equity
    Series and 'T. Rowe Price Mid Cap Growth Portfolio' of Brighthouse Funds Trust I are two funds)."""
    return fund_key(h["holder"]) + "#" + (h.get("holder_cik") or "")
SCHED_FUND = re.compile(r"(?:schedules?|statements?|summary)\s+of\s+(?:portfolio\s+)?investments|portfolio\s+of\s+investments|investment\s+portfolio\b|statements?\s+of\s+net\s+assets", re.I)
_FIL = {}


def _filing(url: str):
    if url not in _FIL:
        import written_proof as WP                    # the visible text of the filing, as the written proof reads it
        _FIL.clear()
        _FIL[url] = WP.Filing(PATHS[url]) if url in PATHS else None
    return _FIL[url]


def heading_fund(r: dict) -> tuple:
    """(name, CIK) of the one known fund named in the heading of the schedule the row is printed in, or None.
    A report can hold the schedules of several funds ("Schedule of Investments December 31, 2019 BlackRock Science and
    Technology Trust (BST)" inside the report of BlackRock Energy & Resources Trust): the row belongs to the fund of its
    own schedule. The heading is the last "Schedule of Investments" (or "Statement of Net Assets") above the row that is
    not part of a sentence ("disclosed in the Schedule of Investments above"); the fund name is read in the 250 characters after it (up to the next table), else in the 150 before it.
    Two different funds there, or none, give None."""
    if r["method"] == "table":
        pos = int(r["table"])
    elif r["method"] == "text layout":
        pos = int(r["row"])
    else:
        return None
    f_ = _filing(r["url"].split("#")[0])
    if f_ is None:
        return None
    v, e = f_.vis(), f_.vpos(pos)
    spans = f_.table_spans()
    hs = [m for m in SCHED_FUND.finditer(v, max(0, e - 300000), e)      # a sentence that refers to it is no heading:
          if not re.search(r"\b(?:the|in|see|our|its|to|of|and|on|each|this|these)\s+(?:(?:consolidated|condensed)\s+)?$", v[max(0, m.start() - 25):m.start()])]   # "disclosed in the Schedule of Investments above"
    if not hs:
        return None
    m = hs[-1]
    nxt = min([a for a, b in spans if a >= m.end()] + [e, m.end() + 250])
    for win in (v[m.end():nxt], v[max(0, m.start() - 150):m.start()]):
        w = " " + fund_key(win) + " "
        found = [k for k in KNOWN if " " + k + " " in w]
        found = [k for k in found if not any(k != o and k in o for o in found)]     # "...Trust" inside "...Trust II"
        if len({KNOWN[k][0] for k in found}) == 1:
            return KNOWN[found[0]]
        if found:
            return None
    return None


def holder(r: dict) -> tuple[str, str, str]:
    """The fund that holds the row: the N-PORT series of the matching row; else the one fund named in the heading of the
    row's own schedule (a fund of the filer, the filer itself, or another fund whose schedule the report also prints);
    else the filer."""
    filer = re.sub(r"\s*\(CIK \d+\)\s*", "", r["filer"]).strip()
    cik = re.search(r"CIK (\d+)", r["filer"])
    fc = cik[1].lstrip("0") if cik else ""
    if r.get("nport_series"):
        return r["nport_series"], "N-PORT series of the matching row", NP_CIK.get(r.get("nport_accession"), "") or fc
    hf = HF.get(r["evidence_id"])
    if hf and fund_key(hf[0]) in SHARED:          # one name, several funds (two "Growth Portfolio"s): the filer's own one, else not named
        own = [x for x in SHARED[fund_key(hf[0])] if x[1] == fc]
        hf = own[0] if own else None
    if hf and r.get("nport_conflict"):
        return hf[0], f"fund named in the heading of the row's schedule (the N-PORT row with the same numbers is of {r['nport_conflict']})", hf[1]
    if hf and fund_key(hf[0]) == fund_key(filer) and hf[1] in ("", fc):
        return filer, "filer, named in the heading of the row's schedule", fc
    if hf and cik and hf[1] == fc:
        return hf[0], "fund named in the heading of the row's schedule, a Form N-PORT series of the filer", fc
    if hf:
        return hf[0], "fund named in the heading of the row's schedule (the report prints the schedules of several funds)", hf[1]
    h = r.get("schedule") or ""
    m = re.search(r"(?:schedules?|statements?|portfolio|summary)\s+of\s+investments|investment\s+portfolio", h, re.I)
    if m and cik:
        before = re.sub(r"\b(?:Table\s+)?of\s+Contents\b|\bConsolidated\b|\bCondensed\b", " ", h[:m.start()], flags=re.I)[-120:]
        names = list(FUNDNAME.finditer(before))
        if names:
            n = key_words(names[-1][1])
            hits = [x for x in SERIES_OF.get(cik[1].lstrip("0"), ()) if n and key_words(x) == n]
            if len(hits) == 1:
                return hits[0], "fund named in the table heading, same as a Form N-PORT series of the filer", fc
    return filer, "filer (the fund inside the filing is not named by rule)", fc


def ptype(r: dict) -> str:
    t = f"{r['name']} {r['raw']} {r['header']}"
    v, c = f(r.get("value_usd")), f(r.get("commitment_usd"))
    if c and not v:
        return "unfunded_commitment"
    if v is not None and v < 0:                    # a loan's fair value is below zero only for its undrawn part (the discount on the commitment)
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
            if f(r.get("qty_usd")) and not v and not f(r.get("cost_usd")):
                return "unfunded_commitment"          # a principal with no cost and no value: the undrawn part of a loan ("First Lien - Undrawn")
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


def subtotals() -> dict:
    """Rows that are the issuer's subtotal, not a position: no shares or principal, and a cost and a value that equal the
    sums of two to four other rows of the same filing and date (a BDC prints "Databricks, Inc." under its term loan and
    delayed-draw lines with their totals). Every evidence row of the filing counts, held back or not; rows printed twice
    (the tag and its table cell) count once."""
    import itertools
    allr = list(csv.DictReader(open(ev / "evidence_rows.csv", encoding="utf-8")))
    by = collections.defaultdict(list)
    for x in allr:
        if not x["problem"] or x["problem"].startswith("two readings"):
            by[(x["accession"], x["report_date"])].append(x)
    near = lambda a, b: abs(a - b) <= max(1.0, abs(b) * 1e-6) + 1000 * (abs(b) >= 1e6)   # noqa: E731  tags in $000s
    out_ = {}
    for x in R:
        c0, v0 = f(x.get("cost_usd")), f(x.get("value_usd"))
        if f(x.get("qty_usd")) or not (c0 and v0):
            continue
        oth = {(f(y.get("qty_usd")), f(y.get("cost_usd")), f(y.get("value_usd"))) for y in by[(x["accession"], x["report_date"])]
               if y["evidence_id"] != x["evidence_id"] and (f(y.get("cost_usd")) or f(y.get("value_usd")))}
        oth.discard((None, c0, v0))
        oth = sorted(oth, key=str)[:12]
        for n in (2, 3, 4):
            hit = next((g for g in itertools.combinations(oth, n) if near(sum(z[1] or 0 for z in g), c0) and near(sum(z[2] or 0 for z in g), v0)
                        and any(z[0] for z in g)), None)
            if hit:
                out_[x["evidence_id"]] = f"issuer subtotal: cost and value are the sums of {n} rows above it in the same filing"
                break
    return out_


import hashlib
import inspect
_hfc = ev / "heading_funds.json"                   # kept between the two runs of this step: reading the headings is slow
_kv = hashlib.sha1(("|".join(sorted(f"{k}={v}" for k, v in KNOWN.items())) + SCHED_FUND.pattern + inspect.getsource(heading_fund)).encode()).hexdigest()
_old = json.loads(_hfc.read_text(encoding="utf-8")) if _hfc.exists() else {}
_old = _old.get("rows", {}) if _old.get("known") == _kv else {}
HF, _new = {}, {}                                  # heading funds, read one filing at a time
for r_ in sorted((x for x in R if x["method"] in ("table", "text layout")), key=lambda x: x["url"]):   # N-PORT-matched rows too: the page heading checks the match
    ck = f'{r_["url"].split("#")[0]}|{r_["method"]}|{r_["table"]}|{r_["row"]}'
    hit = _old[ck] if ck in _old else heading_fund(r_)
    HF[r_["evidence_id"]] = tuple(hit) if hit else None
    _new[ck] = hit
_FIL.clear()
CONFLICT = 0


def other_fund(heading: str, series: str) -> bool:
    """True when the heading names a different fund than the N-PORT series: not the same name, not a short form of it
    ('Growth Fund' in 'Neuberger Large Cap Growth Fund'), and not the umbrella trust that files for the series."""
    a, b = fund_key(heading), fund_key(series)
    if a == b or set(a.split()) <= set(b.split()):
        return False
    return a not in REG_OF.get(b, ())


for r_ in R:                                       # an N-PORT match by shares alone can pick a fund whose schedule the row is not in
    hf = HF.get(r_["evidence_id"])
    if r_.get("nport_series") and hf and other_fund(hf[0], r_["nport_series"]):
        CONFLICT += 1
        print("  N-PORT series", repr(r_["nport_series"]), "but the row sits in the schedule of", repr(hf[0]), "|", r_["accession"], r_["name"][:40], r_.get("qty"), r_.get("value"), r_.get("as_of"))
        r_["nport_conflict"] = r_["nport_series"]
        for k_ in ("nport_series", "nport_asset_cat", "nport_accession"):
            r_[k_] = ""
        if r_["tier"] == "A N-PORT":
            r_["tier"] = "B stated"                    # the printed row stands alone; its scale is proven by written words or the per-share price
        r_["nport_note"] = f"N-PORT match dropped: it named {r_['nport_conflict']}, but the row is printed in the schedule of {hf[0]}"
print(CONFLICT, "N-PORT matches dropped: the row is printed in another fund's schedule")
_hfc.write_text(json.dumps({"known": _kv, "rows": {**_old, **_new}}), encoding="utf-8")
SUBTOTAL = subtotals()
print(len(SUBTOTAL), "issuer subtotal rows left out (not a position)")
with open(ev / "holdings_subtotals.csv", "w", newline="", encoding="utf-8") as _fh:   # every left-out row is listed with its reason
    _w = csv.writer(_fh); _w.writerow(["evidence_id", "why"]); _w.writerows(sorted(SUBTOTAL.items()))
out, seen = [], {}
for r in R:
    if r["evidence_id"] in SUBTOTAL:
        continue
    pt = ptype(r)
    q, c, v, cm = (f(r.get(k)) for k in ("qty_usd", "cost_usd", "value_usd", "commitment_usd"))
    if c is None:                                      # cost printed inside the security's name: "(Acquired 7/24/20, Cost $960,476)"
        ic = INLINE_COST.findall(joined_cost(r["raw"] or r["name"]))
        if len(ic) == 1:                               # one printed cost, in the table's scale ("Cost $835" under "Cost and value in $000s")
            c = float(ic[0].replace(",", "")) * (f(r.get("unit_factor")) or 1)
    hd, hs, hc = holder(r)
    shares = q if pt in ("equity", "spv_interest") else None      # an interest in a vehicle: its units, as printed
    if not any(x for x in (q, c, v, cm)):
        continue                                       # no amount in the row: not a position
    row = dict(holder=hd, holder_source=hs, holder_cik=hc, vehicle="direct" if pt != "spv_interest" else r["name"][:90], position_type=pt, security=security(r),
               shares=shares, par=q if pt in ("funded_loan", "unfunded_commitment") else None, cost_usd=c, fair_value_usd=v, commitment_usd=cm,
               price_per_share=v / shares if v and shares and pt == "equity" else None, cost_per_share=c / shares if c and shares and pt == "equity" else None,
               acquisition_date=acq_date(r), as_of_date=r["report_date"], as_of_source=r["as_of_source"], form=r["form"],
               filer=r["filer"], filing_date=r["file_date"], sec_url=r["url"], evidence_id=r["evidence_id"], tier=r["tier"],
               header=r["header"], unit=r["unit"], raw=r["raw"], table=r["table"], row=r["row"], sources=1, other_evidence_ids="",
               shared_with="", value_scale=1, shares_scale=1, pct_net_assets="", page="", evidence=r["raw"][:200],
               confidence=100, nport_check=r["tier"], repair="")
    k = (fid(row), row["security"], pt, row["as_of_date"], round(v or 0), round(q or 0), round(c or 0))
    if k in seen:                                   # the same position in another filing (annual report and prospectus ...)
        o = seen[k]
        o["sources"] += 1
        if r["file_date"] > o["filing_date"] or (r["tier"] < o["tier"]):
            keep = {"sources": o["sources"], "other_evidence_ids": (o["other_evidence_ids"] + " " + o["evidence_id"]).strip()}   # the row it replaces stays listed
            o.update(row, **keep)
        else:
            o["other_evidence_ids"] = (o["other_evidence_ids"] + " " + r["evidence_id"]).strip()
        continue
    seen[k] = row
    out.append(row)
# one filing that prints the same shares twice (the schedule row, and the row of a "Restricted Securities" note that adds
# the cost and acquisition date): one position. Same filing, fund, type and date (a security named in both rows must be the
# same), and shares and value both printed
# and equal (an interest in a vehicle: the vehicle, cost and value). Loans are left alone: two tranches of one loan can carry the same numbers.
same, kept = {}, []
for h in out:
    spv = h["position_type"] == "spv_interest" and h["cost_usd"] and h["fair_value_usd"]     # an interest in a vehicle: no shares, cost and value
    if (h["position_type"] == "equity" and h["shares"] and h["fair_value_usd"]) or spv:
        k = (h["sec_url"], fid(h), h["position_type"], h["as_of_date"], round(h["shares"] or 0), round(h["fair_value_usd"]),
             (round(h["cost_usd"]), re.sub(r"\W+", " ", h["vehicle"].lower())[:30]) if spv else "")
        if k in same and same[k]["security"] and h["security"] and same[k]["security"] != h["security"]:
            kept.append(h)                          # two named securities (Series D, Series J): two positions
            continue
        if k in same:
            a = same[k]
            full = lambda x: sum(x[y] not in (None, "") for y in ("shares", "cost_usd", "fair_value_usd", "acquisition_date"))   # noqa: E731
            best, other = (a, h) if full(a) >= full(h) else (h, a)
            best["sources"] += other["sources"]
            best["other_evidence_ids"] = (best["other_evidence_ids"] + " " + other["evidence_id"] + " " + other["other_evidence_ids"]).strip()
            best["security"] = best["security"] or other["security"]
            if best is h:
                kept[kept.index(a)] = h
                same[k] = h
            continue
        same[k] = h
    kept.append(h)
print(len(out) - len(kept), "rows that repeat a position of the same filing merged into it")
out = kept
# one report filed by several registrants (JPMorgan files one shareholder report under four trusts): a row held by a filer
# (its fund not named by rule) that prints exactly the numbers of a row of a named fund, same date and type, is that row
named = collections.defaultdict(list)
for h in out:
    if not h["holder_source"].startswith("filer (the"):
        named[(h["as_of_date"], h["position_type"], *(round(h[y] or 0) for y in ("shares", "par", "cost_usd", "fair_value_usd")))].append(h)
kept = []
for h in out:
    tw = named.get((h["as_of_date"], h["position_type"], *(round(h[y] or 0) for y in ("shares", "par", "cost_usd", "fair_value_usd")))) \
        if h["holder_source"].startswith("filer (the") and h["fair_value_usd"] else None
    if tw and len({fid(o) for o in tw}) == 1:
        tw[0]["sources"] += h["sources"]
        tw[0]["other_evidence_ids"] = (tw[0]["other_evidence_ids"] + " " + h["evidence_id"] + " " + h["other_evidence_ids"]).strip()
        continue
    kept.append(h)
print(len(out) - len(kept), "rows held by a filer that repeat a named fund's row of the same report merged into it")
out = kept
# a row that prints the value without shares (the Level 3 valuation table: "Databricks, Inc., Series G | 2,983,823 | Market
# approach ...", or a restricted-securities note: "Databricks - Common Stocks | 7/24/2025 | $ 26,428 | $ 35,690") repeats the
# schedule row of the same filing, fund, date and type that prints shares (or principal) and that same value: one position
# (the other row's id is kept in other_evidence_ids)
full, anyf = collections.defaultdict(list), collections.defaultdict(list)
for h in out:
    if h["fair_value_usd"] and (h["shares"] not in (None, "") or h["par"] not in (None, "")):
        full[(h["sec_url"], fid(h), h["as_of_date"], h["position_type"], round(h["fair_value_usd"]))].append(h)
        anyf[(fid(h), h["as_of_date"], h["position_type"], round(h["fair_value_usd"]))].append(h)
kept = []
for h in out:
    alone = h["fair_value_usd"] and h["shares"] in (None, "") and h["par"] in (None, "") and h["commitment_usd"] in (None, "")
    fits = lambda o: ((o["security"] == h["security"] or not h["security"] or not o["security"])   # noqa: E731
                      and (h["cost_usd"] in (None, "") or o["cost_usd"] in (None, "") or abs(o["cost_usd"] - h["cost_usd"]) < 0.5))
    tw = [o for o in full.get((h["sec_url"], fid(h), h["as_of_date"], h["position_type"], round(h["fair_value_usd"] or 0)), []) if fits(o)] if alone else []
    if alone and not tw:                           # one report filed under several registrants: the schedule row sits in the other filing
        tw = [o for o in anyf.get((fid(h), h["as_of_date"], h["position_type"], round(h["fair_value_usd"] or 0)), []) if fits(o)]
        tw = tw if len({o["sec_url"] for o in tw}) == 1 else []
    if len(tw) == 1:
        o = tw[0]                                   # the schedule row keeps only what its own printed row shows: every number stays provable from one row
        o["sources"] += h["sources"]
        o["other_evidence_ids"] = (o["other_evidence_ids"] + " " + h["evidence_id"] + " " + h["other_evidence_ids"]).strip()
        continue
    kept.append(h)
print(len(out) - len(kept), "rows that print the value of a schedule row (same fund and date) without its shares merged into it")
out = kept
# one position read twice: the iXBRL tags give the undrawn amount of a delayed-draw loan as "principal", the printed table
# puts it in its commitment column; or two filings print it with and without the cost. Same fund, date, type and value
# (not zero), the same amount (principal or commitment), costs not different: one position, from different filings or
# read by different methods (two rows of one table read the same way are two tranches)
dbl = collections.defaultdict(list)
for h in out:
    amt = h["par"] if h["par"] not in (None, "") else h["commitment_usd"]
    if h["fair_value_usd"] and amt not in (None, "") and h["position_type"] in ("funded_loan", "unfunded_commitment"):
        dbl[(fid(h), h["as_of_date"], h["position_type"], round(h["fair_value_usd"]), round(amt))].append(h)
gone = set()
for xs in dbl.values():
    for i, a in enumerate(xs):
        for b in xs[i + 1:]:
            if id(a) in gone or id(b) in gone:
                continue
            same_way = a["sec_url"] == b["sec_url"] and (a["tier"] == "A tag") == (b["tier"] == "A tag")
            if same_way or not (a["cost_usd"] in (None, "") or b["cost_usd"] in (None, "") or abs(a["cost_usd"] - b["cost_usd"]) < 0.5):
                continue
            full_ = lambda x: sum(x[y] not in (None, "") for y in ("par", "cost_usd", "fair_value_usd", "commitment_usd"))   # noqa: E731
            best, other = (a, b) if (full_(a), a["filing_date"]) >= (full_(b), b["filing_date"]) else (b, a)
            best["sources"] += other["sources"]
            best["other_evidence_ids"] = (best["other_evidence_ids"] + " " + other["evidence_id"] + " " + other["other_evidence_ids"]).strip()
            gone.add(id(other))
print(len(gone), "loan rows read twice (iXBRL principal vs printed commitment column, or with and without cost) merged")
out = [h for h in out if id(h) not in gone]
names = collections.defaultdict(collections.Counter)    # one name per fund in the sheet: the spelling used most
for h in out:
    names[fid(h)][h["holder"]] += 1
for h in out:
    h["holder"] = names[fid(h)].most_common(1)[0][0]
clash = collections.defaultdict(set)                    # two funds, one name: the registrant is added to tell them apart
for h in out:
    clash[fund_key(h["holder"])].add(h.get("holder_cik") or "")
for h in out:
    if len(clash[fund_key(h["holder"])]) > 1:
        reg = REG_NAME.get(h.get("holder_cik") or "") or re.sub(r"\s*\(CIK \d+\)\s*", "", h["filer"]).strip()
        if fund_key(reg) != fund_key(h["holder"]):
            h["holder"] = f"{h['holder']} ({reg})"
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
