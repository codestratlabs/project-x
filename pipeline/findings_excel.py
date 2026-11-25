#!/usr/bin/env python3
"""The findings workbook in the shared team layout (About sheet, one sheet per finding type, every value next to the same
value as written in the source, a Match column, the ground truth row or sentence, the printed page, and a link that opens
the filing at the text). Everything comes from the verified evidence files and the structured records; no model is used.

    python findings_excel.py <company> "<Company name>" <output folder>
"""
from __future__ import annotations

import bisect
import csv
import html
import json
import re
import sys
import urllib.parse
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter as L
from openpyxl.workbook.properties import CalcProperties
from openpyxl.worksheet.hyperlink import Hyperlink

csv.field_size_limit(10**9)
co, name, dest = sys.argv[1], sys.argv[2], Path(sys.argv[3])
sys.path.insert(0, str(Path(__file__).resolve().parent))   # pipeline/: paths.py
import paths as px  # noqa: E402

ev = px.OUT / co / "evidence"
rd = lambda p: list(csv.DictReader(open(p, encoding="utf-8"))) if p.exists() else []   # noqa: E731
E = {r["evidence_id"]: r for r in rd(ev / "evidence_rows.csv")}
LOC = {r["id"]: r for r in rd(ev / "locator.csv")}
H = rd(ev / "holdings_from_evidence.csv")
NP = rd(px.DUMPS / f"{co}_nport" / "nport_holdings.csv")
FD = rd(px.OUT / co / "formd_offerings.csv")
DOL = rd(px.INPUTS / co / "data" / "plan_participants.csv")   # the 401(k) plan only (records.py); dol_form5500.csv holds every plan
LOOKF = {r["accession"]: r["why"] for r in rd(ev / "lookalike_filings.csv")}   # filings where company-finder dropped every mention (evidence_build)
FONT, NAVY = "Arial", "1F3A5F"
BLUE, GREEN = "EAF1FB", "EAF6EC"
BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff￾￿]")


def enc(x: str) -> str:
    return urllib.parse.quote(x, safe="").replace("-", "%2D")     # "-" separates the parts of a text fragment


def link(url: str, text: str, prefix: str = "") -> str:
    """Opens the filing scrolled to the text, highlighted. The prefix (the words printed just before) picks the right place
    when the same text appears more than once."""
    if not text or not url.lower().endswith((".htm", ".html")):
        return url
    clean = lambda x: re.sub(r"\s+([,.;:)%])", r"\1", re.sub(r"\(\s+", "(", " ".join(x.split())))   # as the browser shows it   # noqa: E731
    text, prefix = clean(text), clean(prefix)
    return url + "#:~:text=" + (enc(prefix) + "-," if prefix else "") + enc(text)


def cell_prefix(raw: str, prefix: str, text: str) -> str:
    """Keep the prefix only when it sits in the same table cell as the text: a browser matches inside one cell."""
    return prefix if prefix and any(prefix in c and text in c and c.find(prefix) < c.find(text) for c in raw.split("|")) else ""


def printed(raw: str, v) -> str:
    """The number as written in the row (with commas, brackets) that equals the value."""
    if v in ("", None):
        return ""
    v = abs(float(v))
    for c in re.findall(r"\(?\$?\s*\d[\d,]*(?:\.\d+)?\s*\)?", raw):
        n = re.sub(r"[^\d.]", "", c)
        try:
            if abs(float(n) - v) < 0.6:
                return c.strip()
        except ValueError:
            pass
    return ""


def sheet(wb, title, cols, rows, side=()):
    w = wb.create_sheet(title)
    for i, (_, h, wd) in enumerate(cols, 1):
        c = w.cell(1, i, h)
        c.font = Font(name=FONT, bold=True, color="FFFFFF", size=10)
        c.fill = PatternFill("solid", fgColor=NAVY)
        c.alignment = Alignment(wrap_text=True, vertical="center")
        w.column_dimensions[L(i)].width = wd
    w.row_dimensions[1].height = 42
    for j, r in enumerate(rows, 2):
        r["#"] = j - 1
        for i, (k, _, _) in enumerate(cols, 1):
            v = r.get(k, "")
            if isinstance(v, str):
                v = BAD.sub(" ", v)
            c = w.cell(j, i, v if v != "" else None)
            c.font = Font(name=FONT, size=9)
            c.alignment = Alignment(vertical="top", wrap_text=k in ("ground", "found"))
            if k in side:
                c.fill = PatternFill("solid", fgColor=BLUE if side[k] == "p" else GREEN)
            if k == "link" and v:
                base, _, frag = v.partition("#")       # the jump-to text goes in the link's location field: Numbers breaks a "#" inside the address
                c.hyperlink, c.value = Hyperlink(ref=c.coordinate, target=base, location=frag or None), "open"
                c.font = Font(name=FONT, size=9, color="0563C1", underline="single")
    w.freeze_panes = "B2"
    w.auto_filter.ref = f"A1:{L(len(cols))}{len(rows) + 1}"


fmt = lambda v: f"{float(v):,.0f}" if v not in ("", None) else ""   # noqa: E731
wb = Workbook()

# Holdings (filings): each number, the scale and the date as written in the filing, read again from the filing by
# evidence/written_proof.py; Match is computed there from those printed values, never assumed
WP = {r["evidence_id"]: r for r in rd(ev / "written_proof.csv")}
missing = [h["evidence_id"] for h in H if h["evidence_id"] not in WP]
if missing:
    sys.exit(f"written_proof.csv has no row for {len(missing)} holdings: run evidence/written_proof.py {co} first")
hold = []
for h in H:
    l_ = LOC.get(h["evidence_id"], {})
    w_ = WP[h["evidence_id"]]
    hold.append(dict(holder=h["holder"], hsrc=h.get("holder_source", ""), vehicle=h["vehicle"] if h["position_type"] == "spv_interest" else "direct", ptype=h["position_type"].replace("_", " "),
                     security=h["security"], sh=fmt(h["shares"] or h["par"]), sh_w=w_["qty_w"], cost=fmt(h["cost_usd"]), cost_w=w_["cost_w"], fv=fmt(h["fair_value_usd"]), fv_w=w_["value_w"],
                     cm=fmt(h["commitment_usd"]), cm_w=w_["commitment_w"], unit=w_["unit_w"], scale_chk=w_.get("scale_check", ""), asof=h["as_of_date"], asof_src=w_["asof_src"], asof_w=w_["asof_w"],
                     match=w_["match"], proof={"A N-PORT": "also equal to the fund's Form N-PORT filing", "A tag": "iXBRL tag", "A XML": "N-CEN XML"}.get(h["tier"], "printed table"),
                     ground=w_["ground"], form=h["form"], filer=re.sub(r"\s*\(CIK \d+\)", "", h["filer"]), filed=h["filing_date"], page=l_.get("page", ""),
                     search=l_.get("search_text", ""), link=link(h["sec_url"], l_.get("search_text", ""), cell_prefix(w_["ground"], l_.get("prefix", ""), l_.get("search_text", "")))))
HC = [("#", "#", 5), ("holder", "Holder", 26), ("hsrc", "How the holder is named", 22), ("vehicle", "Vehicle", 16), ("ptype", "Type", 12), ("security", "Security", 10),
      ("sh", "Shares / principal (pipeline)", 13), ("sh_w", "Shares / principal as written in the filing", 15), ("cost", "Cost $ (pipeline)", 14), ("cost_w", "Cost as written in the filing", 14),
      ("fv", "Fair value $ (pipeline)", 14), ("fv_w", "Fair value as written in the filing", 14), ("cm", "Undrawn commitment $ (pipeline)", 13), ("cm_w", "Commitment as written", 12),
      ("unit", "Scale as written in the filing", 22), ("scale_chk", "Scale check: value per share vs N-PORT price", 26), ("asof", "As of (pipeline)", 11), ("asof_src", "Where the date is written", 16), ("asof_w", "Date as written in the filing", 34),
      ("match", "Match", 30), ("proof", "Second source", 20), ("ground", "Row as printed in the filing", 60),
      ("form", "Form", 8), ("filer", "Filed by", 24), ("filed", "Filing date", 11),
      ("page", "Page (printed in the filing)", 9), ("search", "Search text", 12), ("link", "Source (opens at the text)", 10)]
side = {"sh": "p", "sh_w": "g", "cost": "p", "cost_w": "g", "fv": "p", "fv_w": "g", "cm": "p", "cm_w": "g", "asof": "p", "asof_w": "g"}
sheet(wb, "Holdings (filings)", HC, hold, side)

# N-PORT holdings (structured records). "As filed" is read from the filing's own N-PORT XML (dumps/<co>_nport/xml/<accession>.xml.gz):
# the <invstOrSec> whose balance and valUSD equal the record, its numbers exactly as the XML writes them.
import gzip as _gz
_XML = {}


def nport_xml(acc: str) -> list:
    if acc not in _XML:
        _XML.clear()
        f = px.DUMPS / f"{co}_nport" / "xml" / f"{acc}.xml.gz"
        out = []
        if f.exists():
            x = _gz.decompress(f.read_bytes()).decode("utf-8", errors="ignore")
            for b in re.findall(r"<(?:\w+:)?invstOrSec>(.*?)</(?:\w+:)?invstOrSec>", x, re.S):
                g = lambda tag: (re.search(rf"<(?:\w+:)?{tag}>([^<]*)</", b) or [None, ""])[1].strip()   # noqa: E731
                out.append(dict(balance=g("balance"), units=g("units"), val=g("valUSD"), pct=g("pctVal"), lvl=(re.search(r'fairValLevel="?([^"<> ]+)', b) or re.search(r"<(?:\w+:)?fairValLevel>([^<]*)<", b) or [None, ""])[1]))
        _XML[acc] = out
    return _XML[acc]


def num_or_none(s):
    """A number written in the XML or the record; None for "N/A" or nothing."""
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def exact(s: str, money: bool = False) -> str:
    """A number as the source writes it, with thousands commas added and no rounding (money keeps at least 2 decimals)."""
    if s in (None, ""):
        return ""
    if num_or_none(s) is None:
        return s                                   # "N/A" stays as written
    neg, s = s.startswith("-"), s.lstrip("-")
    a, _, b = s.partition(".")
    b = b.rstrip("0")
    if money:
        b = (b + "00")[:max(2, len(b))]
    return ("-" if neg else "") + f"{int(a or 0):,}" + (f".{b}" if b else "")


npr = []
n_xml = 0
for r in sorted(NP, key=lambda x: (x["report_date"], x["fund_house"], x["series"])):
    u = r["shares"] or r["par_or_units"]
    hit = next((x for x in nport_xml(r["accession"]) if num_or_none(x["balance"]) is not None and num_or_none(x["val"]) is not None
                and abs(num_or_none(x["balance"]) - (num_or_none(u) or 0)) < 1e-6 and abs(num_or_none(x["val"]) - (num_or_none(r["value_usd"]) or 0)) < 0.005), None)
    if hit:
        n_xml += 1
        uw, vw, pw, lw = f"{exact(hit['balance'])} ({hit['units']})", f"${exact(hit['val'], True)}", hit["pct"], hit["lvl"]
        match = "Yes: units and value equal the filing's N-PORT XML; the as-filed columns show the XML's own numbers"
    else:
        uw, vw, pw, lw = f"{exact(str(u))} ({r['unit']})" if u else "", f"${exact(r['value_usd'], True)}" if r["value_usd"] else "", r["pct_net_assets"], r["fair_value_level"]
        match = "Yes: same as the team N-PORT database (the filing's XML is not on disk; checked on EDGAR 24 Nov 2026)"
    house = r["fund_house"] if r["fund_house"] and r["fund_house"] != "(unmapped)" else r["registrant"]
    npr.append(dict(rd=r["report_date"], house=house, fund=r["series"] or r["registrant"], sec=f"{r['title']} ({r['asset_cat']}, {r['unit']})",
                    units=exact(str(u)) if u else "", units_w=uw, val=exact(r["value_usd"], True) if r["value_usd"] else "", val_w=vw,
                    pct=pw, lvl=lw, ground=f"Filing: Form {r['form']} {r['accession']}", match=match, link=r["sec_url"]))
NC = [("#", "#", 5), ("rd", "Report date", 11), ("house", "Fund house (team database label, not in the filing)", 16), ("fund", "Fund", 28), ("sec", "Security as filed", 30), ("units", "Units (pipeline)", 12),
      ("units_w", "Units as filed", 16), ("val", "Value $ (pipeline)", 14), ("val_w", "Value as filed", 16), ("pct", "% of fund (as filed)", 8), ("lvl", "Fair-value level (as filed)", 8),
      ("match", "Match", 26), ("link", "EDGAR filing", 10)]
sheet(wb, "N-PORT holdings", NC, npr, {"units": "p", "units_w": "g", "val": "p", "val_w": "g"})

# Form D offerings (structured records)
fdr = []
for r in FD:
    acc = r["accession_number"]
    cik = acc.split("-")[0].lstrip("0")
    fdr.append(dict(filed=r["filing_date"], form=r["submission_type"], acc=acc, first=r["sale_date"], first_w=f"{r['sale_date']} (SALE_DATE)",
                    off=fmt(r["total_offering_amount"]), off_w=f"{fmt(r['total_offering_amount'])} (TOTALOFFERINGAMOUNT)" if r["total_offering_amount"] else "",
                    sold=fmt(r["total_amount_sold"]), sold_w=f"{fmt(r['total_amount_sold'])} (TOTALAMOUNTSOLD)" if r["total_amount_sold"] else "",
                    inv=exact(r["total_investors"]), inv_w=f"{exact(r['total_investors'])} (TOTALNUMBERALREADYINVESTED)" if r["total_investors"] else "",
                    match="Yes: every value is the same as in the Form D record", ground=f"SEC Form D data set, accession {acc}",
                    link=f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/"))
DC = [("#", "#", 5), ("filed", "Filed", 11), ("form", "Form", 7), ("acc", "Accession", 22), ("first", "First sale (pipeline)", 12), ("first_w", "First sale as filed", 24),
      ("off", "Offered $ (pipeline)", 15), ("off_w", "Offered as filed", 30), ("sold", "Sold $ (pipeline)", 15), ("sold_w", "Sold as filed", 26), ("inv", "Investors (pipeline)", 10),
      ("inv_w", "Investors as filed", 30), ("match", "Match", 26), ("link", "EDGAR filing", 10)]
sheet(wb, "Form D offerings", DC, fdr, {"first": "p", "first_w": "g", "off": "p", "off_w": "g", "sold": "p", "sold_w": "g", "inv": "p", "inv_w": "g"})

# DOL 401(k) (structured records)
FIELDS = {"5500": ("TOT_ACT_PARTCP_BOY_CNT", "TOT_ACTIVE_PARTCP_CNT"), "5500-SF": ("SF_TOT_ACT_PARTCP_BOY_CNT", "SF_TOT_ACT_PARTCP_EOY_CNT")}
if DOL and "form" not in DOL[0]:                       # written by an older records.py: 5500 and 5500-SF rows cannot be told apart
    print("DOL 401(k): plan_participants.csv has no 'form' column (an older records.py wrote it); DOL field names left out: run sources/records.py again", flush=True)
FIELD = lambda r, i: FIELDS[r["form"]][i] if r.get("form") in FIELDS else "DOL field not recorded: run sources/records.py again"   # noqa: E731
dol = [dict(year=r["plan_year"], plan=r["plan_number"], boy=r["active_boy"], boy_w=f"{r['active_boy']} ({FIELD(r, 0)})",
            act=r["active_eoy"], act_w=f"{r['active_eoy']} ({FIELD(r, 1)})",
            match="Yes: every number is the same as in the DOL field" if r.get("form") in FIELDS else "Not shown: run sources/records.py again",
            link="https://www.dol.gov/agencies/ebsa/about-ebsa/our-activities/public-disclosure/foia/form-5500-datasets") for r in DOL]
OC = [("#", "#", 5), ("year", "Plan year", 9), ("plan", "Plan number", 9), ("boy", "Active participants, start (pipeline)", 14), ("boy_w", "As filed (DOL field)", 30),
      ("act", "Active participants (pipeline)", 14), ("act_w", "As filed (DOL field)", 30), ("match", "Match", 26), ("link", "DOL data set", 10)]
sheet(wb, "DOL 401(k)", OC, dol, {"boy": "p", "boy_w": "g", "act": "p", "act_w": "g"})

# All evidence: every mention of the company the scan found, and what happened to it
LOCALL = LOC


def tidy(x: str) -> str:
    """Text as a reader sees it: a tag cut at either end of a fixed-length piece is removed, never shown."""
    x = re.sub(r'^[^<>]*?"\s*/?>\s*', "", x) if re.match(r'^[^<>]*?"\s*/?>', x) else x
    lt = x.rfind("<")
    if lt >= 0 and x.rfind(">") < lt:
        x = x[:lt]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", x)).strip()

def readable(r: dict) -> str:
    """The evidence as filed, without markup: a table row or sentence as printed; iXBRL tags and N-CEN XML fields as
    'name = value', the value exactly as the filing gives it (iXBRL: with the tag's scale and sign applied)."""
    x = r["raw"]
    if r["method"] == "iXBRL tag" and x.lstrip().startswith("{"):
        d = json.loads(x)
        return "iXBRL tags (scale and sign applied): " + " | ".join(f"{k} = {v:,.10g}" if isinstance(v, (int, float)) else f"{k} = {v}" for k, v in d.items())
    if r["method"] == "N-CEN XML":
        fields = re.findall(r"<(\w+)((?:\s+\w+=\"[^\"]*\")*)\s*(?:/>|>([^<]*)</\1>)", x)
        out = []
        for name, attrs, val in fields:
            if val.strip():
                out.append(f"{name} = {val.strip()}")
            out += [f"{name} {a} = {b}" for a, b in re.findall(r'(\w+)="([^"]*)"', attrs)]
        return "N-CEN XML fields: " + " | ".join(out)
    return tidy(x)


held_ids = {h["evidence_id"] for h in H}
repeat_ids = {x for h in H for x in (h.get("other_evidence_ids") or "").split()}
_acc_of_holding = {x: E[h["evidence_id"]]["accession"] for h in H for x in (h.get("other_evidence_ids") or "").split()}
same_filing_ids = {x for x in repeat_ids if x in E and E[x]["accession"] == _acc_of_holding[x]}
failed = {x["evidence_id"]: x["fail"] for x in rd(ev / "validation_fail.csv")}
conflict_ids = {x["evidence_id"] for x in rd(ev / "holdings_conflicts.csv")}
WHY = lambda r: ("Verified: in Holdings" if r["evidence_id"] in held_ids else   # noqa: E731
                 "Verified: same position as one in Holdings, printed a second time in the same filing (e.g. its restricted-securities note)" if r["evidence_id"] in same_filing_ids else
                 "Verified: same row as one in Holdings, printed again in another filing" if r["evidence_id"] in repeat_ids else
                 "Held back: " + r["why"] if r["tier"] == "C review" else
                 "Not the company: " + LOOKF[r["accession"]] if r["tier"] == "not a position" and r.get("accession") in LOOKF else
                 "Not a position: " + re.sub(r"^not a position:\s*", "", r["why"] or r.get("problem", "")) if r["tier"] == "not a position" else
                 "Held back: failed a check (" + failed[r["evidence_id"]] + ")" if r["evidence_id"] in failed else
                 "Held back: another filing gives this position a different number" if r["evidence_id"] in conflict_ids else
                 "Not a position: no amount in the row" if not any(r.get(k) for k in ("qty", "cost", "value", "commitment")) else
                 "Verified: same row as one in Holdings, printed again in another filing")
allr = []
for r in E.values():
    l_ = LOCALL.get(r["evidence_id"], {})
    allr.append(dict(kind={"table": "table row", "iXBRL tag": "iXBRL tag", "N-CEN XML": "N-CEN XML", "pdf line": "PDF line", "text layout": "text block"}.get(r["method"], r["method"]),
                     form=r["form"], filer=re.sub(r"\s*\(CIK \d+\)", "", r["filer"]), filed=r["file_date"], asof=r.get("report_date") or r["as_of"],
                     text=readable(r), status=WHY(r), page=l_.get("page", ""), link=link(r["url"], l_.get("search_text", ""), cell_prefix(r["raw"], l_.get("prefix", ""), l_.get("search_text", ""))), acc=r["accession"]))
# Mentions outside tables, read again by the filing's structure (evidence/prose_reader.py): the sentence that names the
# company with its whole block, or a name / heading / cell that is not a sentence. Mentions inside hidden XBRL data are
# not on any page and are counted on the About sheet, not listed.
prose = sorted(rd(ev / "prose_sentences.csv"), key=lambda r: (r["url"], int(r["pos"]) if r["pos"].isdigit() else 0))
if prose and "kind" not in prose[0]:
    sys.exit(f"prose_sentences.csv has no kind column: run evidence/prose_reader.py {co} first")
KIND = {"sentence": ("sentence", "Sentence that names the company"), "pdf line": ("PDF text", "Text of a PDF filing that names the company"),
        "heading or label": ("name or heading", "Names the company; not a sentence and no amount next to it"),
        "table cell": ("table cell", "Names the company in a table cell with no amount"),
        "pdf table line": ("PDF table line", "Held back: a line of a PDF table that names the company; its amounts are not read by rule, check by eye")}
n_hidden = 0
for p_ in prose:
    if p_["kind"] == "hidden":
        n_hidden += 1
        continue
    l_ = LOCALL.get(f"S{p_['accession']}:{p_['pos']}", {})
    kind, status = KIND[p_["kind"]]
    allr.append(dict(kind=kind, form=p_["form"], filer=re.sub(r"\s*\(CIK \d+\)", "", p_["filer"]), filed=p_["file_date"], asof=p_["near_heading_date"],
                     text=p_["sentence"][:1000], status=f"Not the company: {LOOKF[p_['accession']]}" if p_["accession"] in LOOKF else status, para=p_["block"][:4000] if p_["kind"] == "sentence" else "",
                     page=l_.get("page", ""), link=link(p_["url"], l_.get("search_text", "")), acc=p_["accession"]))
allr.sort(key=lambda r: (r["filed"], r["acc"]))
AC = [("#", "#", 6), ("kind", "Kind", 11), ("form", "Form", 8), ("filer", "Filed by", 28), ("filed", "Filing date", 11), ("asof", "As of", 11),
      ("text", "Evidence (table row or sentence, as printed)", 80), ("para", "Whole paragraph (sentences only)", 90), ("status", "What happened to it", 44), ("page", "Page", 9), ("link", "Source (opens at the text)", 10)]
sheet(wb, "All evidence", AC, allr)

# About
s = wb.active
s.title = "About"
s.sheet_view.showGridLines = False
s["A1"] = f"{name}: findings and their ground truth"
s["A1"].font = Font(name=FONT, size=16, bold=True)
s["A2"] = ("Every value comes from fixed rules; no language model is used in this workbook, except the mention finder's look-alike check: "
           "All evidence rows marked 'Not the company' are in filings where the finder dropped every mention, by that check or by its name rule")
s["A2"].font = Font(name=FONT, size=9)
s["A4"] = "Each finding sits next to its ground truth: the exact table row, sentence or structured record it was taken from, with every value as written in the source."
s["A4"].font = Font(name=FONT, size=10)
head = ["Sheet", "Source (form / data set)", "What it gives", "How it was read", "Rows", "How it is checked against the ground truth"]
for i, h in enumerate(head, 1):
    c = s.cell(6, i, h)
    c.font = Font(name=FONT, bold=True, color="FFFFFF", size=10)
    c.fill = PatternFill("solid", fgColor=NAVY)
about = [("Holdings (filings)", "Fund filings (N-CSR, N-CSRS, N-2, 424B3, 10-Q, 10-K): schedules of investments", "Shares or principal, cost, fair value, undrawn commitment of each fund's position",
          "Rules: printed column header, unit and date; iXBRL tags; on converted PDF pages, the column label printed above each number", "Every number written in the table row; unit and date printed; two independent readings of the row agree; loan sums and N-PORT price consistent; equal to Form N-PORT where the fund files it"),
         ("N-PORT holdings", "SEC Form N-PORT (monthly fund holdings)", "Funds holding the company, units, value", "Structured record, no model", "Each value compared with the filing record, field by field"),
         ("Form D offerings", "SEC Form D data set (the company's own notices)", "Date of first sale, amount offered and sold, investors", "Structured record, no model", "Each value compared with the filing record, field by field"),
         ("DOL 401(k)", "Department of Labor Form 5500 (or 5500-SF for a small plan): the company's 401(k) plan", "Plan participants (U.S. staff minimum)", "Structured record, no model", "Each number compared with the original DOL field"),
         ("All evidence", "Every filing that names the company", "Every mention found on a page: each table row, iXBRL tag, N-CEN field, sentence and name, and what happened to it",
          "Scan of every filing for the name; text read by the filing's structure (paragraph, cell, line of a converted PDF page)",
          f"Status column: verified, held back (with the reason), not a position, or not the company (a look-alike name). {n_hidden} mentions sit inside hidden XBRL data (not on any page) and are not listed"),]
for j, row in enumerate(about, 7):
    for i, v in enumerate(row[:4] + (f"=COUNTA('{row[0]}'!A:A)-1",) + row[4:], 1):
        c = s.cell(j, i, v)
        c.font = Font(name=FONT, size=9)
        c.alignment = Alignment(wrap_text=True, vertical="top")
r0 = 7 + len(about) + 1
s.cell(r0, 1, "How to read the comparison").font = Font(name=FONT, size=11, bold=True)
for k, (a, b) in enumerate([("Side by side", "Every value the pipeline produced (blue) sits next to the same value as written in the source (green)."),
                            ("Match", "Code reads the filing again and compares: Yes = every number (with its sign) times the printed scale equals the pipeline value, and the date is printed where shown. No = which part differs."),
                            ("Page", "The page number printed in the filing; '(counted)' where the filing prints none; web-style filings have no pages."),
                            ("Source link", "Opens the filing on SEC EDGAR scrolled to the number, highlighted (text-fragment link; Chrome, Edge, Safari)."),
                            ("Scale", "The words that set the scale (\"in thousands\", \"Value ($)\") as printed in the column header, the table's own title, or the heading of the schedule the row is in; words of other tables never count. For shares, value per share is also checked against the funds' Form N-PORT price that month (a thousands/dollars mix-up would be 1,000x off)."),
                            ("Holder", "The fund of the matching Form N-PORT row; else the one fund named in the heading of the schedule the row is printed in (a report can print the schedules of several funds: a row in another fund's schedule belongs to that fund, not to the filer); else the filer. One position printed in several filings is one row."),
                            ("Held back", "Rows that failed a check are not in Holdings; they are in All evidence, marked 'Held back' with the reason.")], r0 + 1):
    s.cell(k, 1, a).font = Font(name=FONT, size=9, bold=True)
    s.cell(k, 2, b).font = Font(name=FONT, size=9)
for col, wd in zip("ABCDEF", (22, 44, 40, 34, 8, 60)):
    s.column_dimensions[col].width = wd
first = ["About", "All evidence"]
wb._sheets = [wb[n] for n in first] + [x for x in wb._sheets if x.title not in first]
wb.active = 0
wb.calculation = CalcProperties(fullCalcOnLoad=True)
dest.mkdir(parents=True, exist_ok=True)
out = dest / f"{name.split(',')[0].replace(' ', '_')}_findings.xlsx"
wb.save(out)
print(out, {"holdings": len(hold), "nport": len(npr), "formd": len(fdr), "dol": len(dol), "all evidence": len(allr), "hidden XBRL mentions": n_hidden,
            "holdings not matched": sum(1 for h in hold if h["match"].startswith("No"))})
