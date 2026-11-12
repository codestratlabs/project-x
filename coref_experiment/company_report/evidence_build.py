#!/usr/bin/env python3
"""Sort parsed rows by what proves them. N-PORT can confirm or flag a row; it never sets a value or a unit.

    A  tag         read from an iXBRL tag in the filing
    A  N-PORT      value (and shares where printed) equal a Form N-PORT row of the same date
    B  stated      column header and unit printed, no second source
    C  review      no unit printed and no second source, per-share value out of range, or differs from N-PORT
"""
import collections, csv, datetime as dt, json, re, sys
from pathlib import Path
csv.field_size_limit(10**8)
co = sys.argv[1]
HERE = Path(__file__).parent
ev = HERE / "out" / co / "evidence"
cfg = json.loads((HERE / "model_config" / f"{co}.json").read_text(encoding="utf-8"))
day_, ratio = cfg.get("split_date", "0"), cfg.get("split_ratio", 1)
P = list(csv.DictReader(open(ev / "parsed_rows.csv", encoding="utf-8")))
np_ = list(csv.DictReader(open(HERE.parents[2] / "dumps" / f"{co}_nport" / "nport_holdings.csv", encoding="utf-8")))
by = collections.defaultdict(list)
for r in np_:
    by[r["report_date"]].append((float(r["value_usd"] or 0), float(r["shares"] or r["par_or_units"] or 0), r["series"] or r["registrant"], r["asset_cat"], r["accession"], r["cik"].lstrip("0")))
by_shares = collections.defaultdict(list)          # shares -> (value, date): to test the date of rows with no second source
for d_, xs in by.items():
    for x in xs:
        if x[1]:
            by_shares[round(x[1])].append((x[0], d_))
marks = {r["month"]: float(r["median_price"]) for r in csv.DictReader(open(HERE / "inputs" / co / "data" / "nport_price_monthly.csv", encoding="utf-8"))}


def f(x):
    return float(x) if x not in ("", None) else None


def iso(s):
    s = " ".join((s or "").split()).replace(" ,", ",")
    for fmt in ("%B %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


out = []
for r in P:
    if r["problem"]:
        out.append({**r, "tier": "not a position" if r["problem"].startswith(("percent", "not a position")) else "C review", "why": r["problem"]})
        continue
    u = f(r["unit_factor"]) or 1
    q, c, v = f(r["qty"]), f(r["cost"]), f(r["value"])
    qty = q * (f(r.get("qty_factor")) or 1) if q else None
    val, cost = v * u if v else None, c * u if c else None
    d = iso(r["as_of"])
    r.update(qty_usd=qty, cost_usd=cost, value_usd=val, commitment_usd=f(r.get("commitment")) * u if f(r.get("commitment")) else None, report_date=d)
    why, tier = "", ""
    if r["method"] == "iXBRL tag":
        tier = "A tag"
    elif r["method"] == "N-CEN XML":
        tier = "A XML"
    elif r["method"] == "pdf line":
        tier, why = "C review", "PDF line: columns must be read by eye"
    else:
        fcik = (re.search(r"CIK (\d+)", r["filer"]) or [None, ""])[1].lstrip("0")
        rows = by.get(d, [])
        tol = max(1.0, u * 0.51)
        vm = [x for x in rows if val and abs(x[0] - val) <= tol]
        qm = [x for x in vm if not qty or abs(x[1] - qty) <= max(1.0, x[1] * 0.0005)]
        same = [x for x in qm if x[5] == fcik]
        if qm and not same and not qty:
            qm = []                                     # a value alone equal to another registrant's row proves nothing
        if qm and not same:
            r["nport_note"] = "equals the N-PORT row of another registrant: " + "; ".join(sorted({x[2] for x in qm}))[:200]
        if vm and qm:
            tier = "A N-PORT"
            if same and len({x[2] for x in same}) == 1:   # the fund is named only from the filer's own N-PORT, one series
                r.update(nport_series=same[0][2], nport_asset_cat=same[0][3], nport_accession=same[0][4])
        elif val and rows and any(qty and abs(x[1] - qty) <= max(1.0, x[1] * 0.0005) for x in rows):
            tier, why = "C review", "same shares as an N-PORT row, different value"
        else:
            m = None
            if marks and val and qty and d:
                k = min(marks, key=lambda x: abs(int(x[:4]) * 12 + int(x[5:]) - int(d[:4]) * 12 - int(d[5:7])))
                m = marks[k]
            loan = val and qty and 0.7 <= val / qty <= 1.3      # par and value: a loan, not shares
            if m and not loan and not m / 3 <= val / qty / (ratio if d < day_ else 1) <= m * 3:
                tier, why = "C review", "per-share value out of range"
            elif r["unit"].startswith("no unit"):
                tier, why = "C review", "no unit printed, no second source"
            else:
                tier = "B stated"
    out.append({**r, "tier": tier, "why": why})
# a table with a row confirmed by N-PORT has its unit proven for the other rows of that table
proven = {(r["accession"], r["table"]) for r in out if r["tier"] == "A N-PORT"} | {(r["accession"], r["header"]) for r in out if r["tier"] == "A N-PORT"}
for r in out:
    if r["why"].startswith("no unit printed") and ((r["accession"], r["table"]) in proven or (r["accession"], r["header"]) in proven):
        r["tier"], r["why"] = "B stated", ""
        r["unit"] = "dollars, proven by another row of this filing with the same header"
# date test for rows with no second source: the same shares and value in N-PORT at another date means the date rule failed
for r in out:
    if r["tier"] != "B stated" or not r.get("qty_usd") or not r.get("value_usd"):
        continue
    q_, v_ = float(r["qty_usd"]), float(r["value_usd"])
    tol = max(1.0, (f(r["unit_factor"]) or 1) * 0.51)
    other = sorted({d_ for val_, d_ in by_shares.get(round(q_), []) if abs(val_ - v_) <= tol and d_ != r["report_date"]})
    if other:
        r["tier"], r["why"] = "C review", "same shares and value as an N-PORT row of another date (" + ", ".join(other[:3]) + "): the date may be wrong"
# two readings of the row (by column position, and right-aligned with the header) must agree; a row read only one way
# must have a single amount, so there is nothing to mix up. N-PORT stands in for the second reading of shares and value.
def amounts_in(raw: str, name: str) -> int:
    cells = [c.strip() for c in raw.split("|")]
    return sum(1 for c in cells if c != name.strip() and re.fullmatch(r"[-−–]?\(?\$?\s*\(?\s*\d[\d,]*(?:\.\d+)?\s*\)?", c) and not re.fullmatch(r"\d{1,2}", c))


for r in out:
    if r["method"] != "table" or r["tier"] not in ("A N-PORT", "B stated"):
        continue
    m_ = r.get("methods") or ""
    if m_.startswith("two readings differ"):
        diff = set(re.findall(r"(qty|cost|value|commitment) ", m_))
        if not (r["tier"] == "A N-PORT" and diff <= {"qty", "value"}):
            r["tier"], r["why"] = "C review", m_[:200]
    elif m_.startswith("one reading") and r["tier"] == "B stated" and amounts_in(r["raw"], r["name"]) > 1:
        r["tier"], r["why"] = "C review", "read one way only and the row has more than one amount"
# a number with no date cannot be placed in a year-by-year table
for r in out:
    if r["tier"] in ("A tag", "A N-PORT", "A XML", "B stated") and not r.get("report_date"):
        r["tier"], r["why"] = "C review", "no date found for the table"
import hashlib
for r in out:
    r["evidence_id"] = "E" + hashlib.sha1("|".join((r["accession"], str(r["table"]), str(r["row"]), r["name"], r["method"])).encode()).hexdigest()[:10]
head = ["evidence_id"] + list(P[0]) + ["nport_series", "nport_asset_cat", "nport_accession", "nport_note", "qty_usd", "cost_usd", "value_usd", "commitment_usd", "report_date", "tier", "why"]
with open(ev / "evidence_rows.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, head, extrasaction="ignore")
    w.writeheader()
    w.writerows(out)
t = collections.Counter(r["tier"] for r in out)
print(t, collections.Counter(r["why"] for r in out).most_common(8))
print(collections.Counter((r["form"][:6], r["tier"]) for r in out if r["tier"]).most_common(16))
print({k: sum(1 for r in out if r["tier"] == "A N-PORT" and k in r["unit"]) for k in ("no unit", "ousand", "000")})
print(collections.Counter(r["filer"].split()[0] for r in out if r["tier"] == "C review").most_common(10))
