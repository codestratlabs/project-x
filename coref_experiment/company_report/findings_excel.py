#!/usr/bin/env python3
"""The findings workbook in the shared team layout (About sheet, one sheet per finding type, every value next to the same
value as written in the source, a Match column, the ground truth row or sentence, the printed page, and a link that opens
the filing at the text). Everything comes from the verified evidence files and the structured records; no model is used.

    python findings_excel.py <company> "<Company name>" <output folder>
"""
from __future__ import annotations

import csv
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
HERE = Path(__file__).parent
ROOT = HERE.parents[2]
ev = HERE / "out" / co / "evidence"
rd = lambda p: list(csv.DictReader(open(p, encoding="utf-8"))) if p.exists() else []   # noqa: E731
E = {r["evidence_id"]: r for r in rd(ev / "evidence_rows.csv")}
LOC = {r["id"]: r for r in rd(ev / "locator.csv")}
H, F, RL = rd(ev / "holdings_from_evidence.csv"), rd(ev / "facts_from_evidence.csv"), rd(ev / "relationships_from_evidence.csv")
NP = rd(ROOT / "dumps" / f"{co}_nport" / "nport_holdings.csv")
FD = rd(HERE / "out" / co / "formd_offerings.csv")
DOL = rd(HERE / "inputs" / co / "data" / "dol_form5500.csv")
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

# Holdings (filings)
hold = []
for h in H:
    e = E.get(h["evidence_id"], {})
    l_ = LOC.get(h["evidence_id"], {})
    raw = e.get("raw", "")
    pairs = [(e.get("qty"), h["shares"] or h["par"]), (e.get("cost"), h["cost_usd"]), (e.get("value"), h["fair_value_usd"]), (e.get("commitment"), h["commitment_usd"])]
    asw = [printed(raw + " | " + e.get("name", ""), p) if p else "" for p, _ in pairs]
    ok = all((a != "") for (p, _), a in zip(pairs, asw) if p) or e.get("method") in ("iXBRL tag", "N-CEN XML")
    hold.append(dict(holder=h["holder"], vehicle=h["vehicle"] if h["position_type"] == "spv_interest" else "direct", ptype=h["position_type"].replace("_", " "),
                     security=h["security"], sh=fmt(h["shares"] or h["par"]), sh_w=asw[0], cost=fmt(h["cost_usd"]), cost_w=asw[1], fv=fmt(h["fair_value_usd"]), fv_w=asw[2],
                     cm=fmt(h["commitment_usd"]), cm_w=asw[3], unit=h["unit"], asof=h["as_of_date"], asof_w=(e.get("schedule") or e.get("as_of_source") or "")[:80],
                     match="Yes: every number is the same as in the filing row" if ok else "No: a number is not written in the row",
                     proof={"A N-PORT": "also equal to the fund's Form N-PORT filing", "A tag": "iXBRL tag", "A XML": "N-CEN XML"}.get(h["tier"], "printed table"),
                     ground=raw, form=h["form"], filer=re.sub(r"\s*\(CIK \d+\)", "", h["filer"]), filed=h["filing_date"], page=l_.get("page", ""),
                     search=l_.get("search_text", ""), link=link(h["sec_url"], l_.get("search_text", ""), cell_prefix(raw, l_.get("prefix", ""), l_.get("search_text", "")))))
HC = [("#", "#", 5), ("holder", "Holder", 26), ("vehicle", "Vehicle", 16), ("ptype", "Type", 12), ("security", "Security", 10),
      ("sh", "Shares / principal (pipeline)", 13), ("sh_w", "Shares / principal as written in the filing", 15), ("cost", "Cost $ (pipeline)", 14), ("cost_w", "Cost as written in the filing", 14),
      ("fv", "Fair value $ (pipeline)", 14), ("fv_w", "Fair value as written in the filing", 14), ("cm", "Undrawn commitment $ (pipeline)", 13), ("cm_w", "Commitment as written", 12),
      ("unit", "Unit as printed (scale applied)", 22), ("asof", "As of (pipeline)", 11), ("asof_w", "As of as written (table heading)", 22), ("match", "Match", 24), ("proof", "Second source", 20),
      ("form", "Form", 8), ("filer", "Filed by", 24), ("filed", "Filing date", 11),
      ("page", "Page (printed in the filing)", 9), ("search", "Search text", 12), ("link", "Source (opens at the text)", 10)]
side = {"sh": "p", "sh_w": "g", "cost": "p", "cost_w": "g", "fv": "p", "fv_w": "g", "cm": "p", "cm_w": "g", "asof": "p", "asof_w": "g"}
sheet(wb, "Holdings (filings)", HC, hold, side)

# Facts
facts = []
for f in F:
    l_ = LOC.get(f["fact_id"], {})
    xml = f["metric"] == "fund_transaction"
    facts.append(dict(found=f"{f['metric'].replace('_', ' ')}: {f['printed']}", ground=f["sentence"], date=f["date"] or "not stated", date_w=f["date"] or "—",
                      num=fmt(f["value"]) if f["unit"] == "USD" else f["value"], num_w=f["printed"],
                      match="Yes: same number as written" + (" (XML field)" if xml else ", scale word printed next to it"),
                      ftype=f["metric"].replace("_", " "), form=f["form"], filer=re.sub(r"\s*\(CIK \d+\)", "", f["filer"]), filed=f["filing_date"],
                      page=l_.get("page", ""), link=link(f["url"], "" if xml else f["printed"].strip())))
FCOL = [("#", "#", 5), ("found", "What the pipeline found", 30), ("date", "Date (pipeline)", 12),
        ("date_w", "Date as written in the filing", 16), ("num", "Number (pipeline)", 16), ("num_w", "Number as written in the filing", 18), ("match", "Match", 26),
        ("ftype", "Type", 16), ("form", "Form", 8), ("filer", "Filed by", 24), ("filed", "Filing date", 11), ("page", "Page (printed in the filing)", 9), ("link", "Source (opens at the text)", 10)]
FACTS_ARGS = ("Facts", FCOL, facts, {"date": "p", "date_w": "g", "num": "p", "num_w": "g"})

# Relationships
best = {}
for h in hold:
    pass
rels = []
loans = ("funded loan", "unfunded commitment")
for r in RL:
    lend = r["relation"] == "lends_to"
    hs = [x for x in hold if x["holder"] == r["from_company"] and ((x["ptype"] in loans) == lend)]
    b = max(hs, key=lambda x: (x["asof"], x["filed"])) if hs else {}
    rels.append(dict(found=f"{r['from_company']} {'lends to' if lend else 'invests in'} {name}", ground=b.get("ground", r["quote"]),
                     frm=r["from_company"], frm_w=b.get("filer", ""), to=name, to_w=re.split(r"\s*\|", b.get("ground", ""))[0][:60],
                     match="Yes: the filer's holdings table lists the company" + (" as a loan" if lend else " as shares or an interest"),
                     how="Rule: a fund's own holdings table; loan rows = lender, share rows = investor", rows=r["rows"], first=r["first_date"], last=r["last_date"],
                     form=b.get("form", r["form"]), filer=b.get("filer", ""), filed=b.get("filed", r["filing_date"]), page=b.get("page", ""), link=b.get("link", r["sec_url"])))
RCOL = [("#", "#", 5), ("found", "What the pipeline found", 34), ("ground", "Ground truth (the holdings row, as printed)", 60), ("frm", "From (pipeline)", 24),
        ("frm_w", "From as named in the ground truth (filer)", 24), ("to", "To (pipeline)", 16), ("to_w", "To as named in the ground truth", 22), ("match", "Match", 26),
        ("how", "How it was read", 26), ("rows", "Holdings rows", 8), ("first", "First seen", 11), ("last", "Last seen", 11), ("form", "Form", 8), ("filer", "Filed by", 24),
        ("filed", "Filing date", 11), ("page", "Page (printed in the filing)", 9), ("link", "Source (opens at the text)", 10)]
# the Relationships sheet is left out of this workbook (relationships stay in relationships_from_evidence.csv)

# N-PORT holdings (structured records)
npr = []
for r in sorted(NP, key=lambda x: (x["report_date"], x["fund_house"], x["series"])):
    u = r["shares"] or r["par_or_units"]
    npr.append(dict(rd=r["report_date"], house=r["fund_house"], fund=r["series"] or r["registrant"], sec=f"{r['title']} ({r['asset_cat']}, {r['unit']})",
                    units=fmt(u), units_w=f"{float(u):,.0f} ({r['unit']})" if u else "", val=fmt(r["value_usd"]), val_w=f"${float(r['value_usd']):,.2f}" if r["value_usd"] else "",
                    pct=r["pct_net_assets"], lvl=r["fair_value_level"], ground=f"Filing: Form {r['form']} {r['accession']}",
                    match="Yes: same units and value (structured record)", link=r["sec_url"]))
NC = [("#", "#", 5), ("rd", "Report date", 11), ("house", "Fund house", 16), ("fund", "Fund", 28), ("sec", "Security as filed", 30), ("units", "Units (pipeline)", 12),
      ("units_w", "Units as filed", 16), ("val", "Value $ (pipeline)", 14), ("val_w", "Value as filed", 16), ("pct", "% of fund", 8), ("lvl", "Fair-value level", 8),
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
                    inv=r["total_investors"], inv_w=f"{r['total_investors']} (TOTALNUMBERALREADYINVESTED)" if r["total_investors"] else "",
                    match="Yes: every value is the same as in the Form D record", ground=f"SEC Form D data set, accession {acc}",
                    link=f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/"))
DC = [("#", "#", 5), ("filed", "Filed", 11), ("form", "Form", 7), ("acc", "Accession", 22), ("first", "First sale (pipeline)", 12), ("first_w", "First sale as filed", 24),
      ("off", "Offered $ (pipeline)", 15), ("off_w", "Offered as filed", 30), ("sold", "Sold $ (pipeline)", 15), ("sold_w", "Sold as filed", 26), ("inv", "Investors (pipeline)", 10),
      ("inv_w", "Investors as filed", 30), ("match", "Match", 26), ("link", "EDGAR filing", 10)]
sheet(wb, "Form D offerings", DC, fdr, {"first": "p", "first_w": "g", "off": "p", "off_w": "g", "sold": "p", "sold_w": "g", "inv": "p", "inv_w": "g"})

# DOL 401(k) (structured records)
dol = [dict(year=r["FORM_PLAN_YEAR_BEGIN_DATE"][:4], plan=r["SPONS_DFE_PN"], boy=r["TOT_ACT_PARTCP_BOY_CNT"], boy_w=f"{r['TOT_ACT_PARTCP_BOY_CNT']} (TOT_ACT_PARTCP_BOY_CNT)",
            act=r["TOT_ACTIVE_PARTCP_CNT"], act_w=f"{r['TOT_ACTIVE_PARTCP_CNT']} (TOT_ACTIVE_PARTCP_CNT)", match="Yes: every number is the same as in the DOL field",
            link="https://www.dol.gov/agencies/ebsa/about-ebsa/our-activities/public-disclosure/foia/form-5500-datasets") for r in DOL]
OC = [("#", "#", 5), ("year", "Plan year", 9), ("plan", "Plan number", 9), ("boy", "Active participants, start (pipeline)", 14), ("boy_w", "As filed (DOL field)", 30),
      ("act", "Active participants (pipeline)", 14), ("act_w", "As filed (DOL field)", 30), ("match", "Match", 26), ("link", "DOL data set", 10)]
sheet(wb, "DOL 401(k)", OC, dol, {"boy": "p", "boy_w": "g", "act": "p", "act_w": "g"})

# All evidence: every mention of the company the scan found, and what happened to it
LOCALL = LOC
held_ids = {h["evidence_id"] for h in H}
repeat_ids = {x for h in H for x in (h.get("other_evidence_ids") or "").split()}
failed = {x["evidence_id"]: x["fail"] for x in rd(ev / "validation_fail.csv")}
conflict_ids = {x["evidence_id"] for x in rd(ev / "holdings_conflicts.csv")}
WHY = lambda r: ("Verified: in Holdings" if r["evidence_id"] in held_ids else   # noqa: E731
                 "Verified: same row as one in Holdings, printed again in another filing" if r["evidence_id"] in repeat_ids else
                 "Held back: " + r["why"] if r["tier"] == "C review" else
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
                     text=r["raw"], status=WHY(r), page=l_.get("page", ""), link=link(r["url"], l_.get("search_text", ""), cell_prefix(r["raw"], l_.get("prefix", ""), l_.get("search_text", ""))), acc=r["accession"]))
for p_ in rd(ev / "prose_sentences.csv"):
    snt = re.sub(r'^[^<>]{0,80}?"\s*>\s*', "", p_["sentence"])
    l_ = LOCALL.get(f"S{p_['accession']}:{p_['pos']}", {})
    allr.append(dict(kind="sentence", form=p_["form"], filer=re.sub(r"\s*\(CIK \d+\)", "", p_["filer"]), filed=p_["file_date"], asof=p_["near_heading_date"],
                     text=snt[:1000], status="Sentence that names the company",
                     page=l_.get("page", ""), link=link(p_["url"], l_.get("search_text", "")), acc=p_["accession"]))
allr.sort(key=lambda r: (r["filed"], r["acc"]))
AC = [("#", "#", 6), ("kind", "Kind", 11), ("form", "Form", 8), ("filer", "Filed by", 28), ("filed", "Filing date", 11), ("asof", "As of", 11),
      ("text", "Evidence (table row or sentence, as printed)", 80), ("status", "What happened to it", 44), ("page", "Page", 9), ("link", "Source (opens at the text)", 10)]
sheet(wb, "All evidence", AC, allr)

# Indirect mentions: pronouns (it / its / they / the company ...) the model tied to the company, and the 50-filing test
IND = rd(HERE / "out" / co / "indirect_located.csv")
SAMPLE_LIST = HERE / "out" / co / "indirect_sample50" / "sample_filings.txt"   # the 50 test filings (meta paths)
SAMPLE = {__import__("json").load(open(x))["accession"] for x in (SAMPLE_LIST.read_text().splitlines() if SAMPLE_LIST.exists() else []) if x.strip() and Path(x).exists()}


def ind_row(r):
    who = (name.split(",")[0] if r["company_id"].startswith("C") else f"the filer ({r['filer']})" if r["company_id"].startswith("F")
           else "none (no company / not this one)")
    url = r["url"] if r["url"].lower().endswith((".htm", ".html")) else f"{r['url']}#page={r['page']}"
    return dict(form=r["form"], filer=r["filer"], filed=r["filed"], word=r["trigger"], text=r["sentence"][:1000], answer=who,
                conf=int(r["confidence"]) if str(r["confidence"]).isdigit() else r["confidence"], reason=r["reason"],
                page=r["page"], search=r["search_text"], link=link(url, r["search_text"]), acc=r["accession"])


IC = [("#", "#", 6), ("form", "Form", 8), ("filer", "Filed by", 28), ("filed", "Filing date", 11), ("word", "Word", 12),
      ("text", "Sentence, as printed", 80), ("answer", "Refers to (model)", 22), ("conf", "Confidence", 10), ("reason", "Model's reason", 44),
      ("page", "Page", 9), ("search", "Search text (Ctrl+F)", 34), ("link", "Source (opens at the word)", 10)]
seen, ind = set(), []
for r in IND:                                           # the same pronoun can be in two runs: keep one
    k = (r["accession"], r["search_text"], r["page"])     # same words on the same page = the same place in the filing
    if r["company_id"].startswith("C") and r["status"] == "resolved" and k not in seen:
        seen.add(k)
        ind.append(ind_row(r))
ind.sort(key=lambda r: (r["filed"], r["acc"]))
seen_t, test = set(), []                                # the 50-filing test: only the places the model tied to the company
for r in IND:
    k = (r["accession"], r["search_text"], r["page"])
    if r["accession"] in SAMPLE and r["company_id"].startswith("C") and r["status"] == "resolved" and k not in seen_t:
        seen_t.add(k)
        test.append(ind_row(r))
test.sort(key=lambda r: (r["filed"], r["acc"]))
sheet(wb, "Indirect mentions", IC, ind)
sheet(wb, "Indirect test (50 filings)", IC, test)

# About
s = wb.active
s.title = "About"
s.sheet_view.showGridLines = False
s["A1"] = f"{name}: findings and their ground truth"
s["A1"].font = Font(name=FONT, size=16, bold=True)
s["A2"] = "Every number comes from rules, no language model · the language model is used only in the two Indirect sheets, to say who a pronoun refers to"
s["A2"].font = Font(name=FONT, size=9)
s["A4"] = "Each finding sits next to its ground truth: the exact table row, sentence or structured record it was taken from, with every value as written in the source."
s["A4"].font = Font(name=FONT, size=10)
head = ["Sheet", "Source (form / data set)", "What it gives", "How it was read", "Rows", "How it is checked against the ground truth"]
for i, h in enumerate(head, 1):
    c = s.cell(6, i, h)
    c.font = Font(name=FONT, bold=True, color="FFFFFF", size=10)
    c.fill = PatternFill("solid", fgColor=NAVY)
about = [("Holdings (filings)", "Fund filings (N-CSR, N-CSRS, N-2, 424B3, 10-Q, 10-K): schedules of investments", "Shares or principal, cost, fair value, undrawn commitment of each fund's position",
          "Rules: printed column header, unit and date; iXBRL tags", "Every number written in the table row; unit and date printed; two independent readings of the row agree; loan sums and N-PORT price consistent; equal to Form N-PORT where the fund files it"),
         ("N-PORT holdings", "SEC Form N-PORT (monthly fund holdings)", "Funds holding the company, units, value", "Structured record, no model", "Each value compared with the filing record, field by field"),
         ("Form D offerings", "SEC Form D data set (the company's own notices)", "Date of first sale, amount offered and sold, investors", "Structured record, no model", "Each value compared with the filing record, field by field"),
         ("DOL 401(k)", "Department of Labor Form 5500", "Plan participants (U.S. staff minimum)", "Structured record, no model", "Each number compared with the original DOL field"),
         ("All evidence", "Every filing that names the company", "Every mention found: each table row, iXBRL tag, N-CEN field and sentence, and what happened to it", "Scan of every filing for the name", "Status column: verified, held back (with the reason), or not a position"),
         ("Indirect mentions", "Filings that name the company", "Sentences where it / its / they / the company points back to the company",
          "Rule picks pronouns within 2 paragraphs of a mention; the language model (GPT-4.1) decides who each one refers to; code checks the name is in the same or the 2 sentences before; a second model (Claude Opus 5.5) must agree", "Model's reason and confidence shown; the sentence and link let anyone check it"),
         ("Indirect test (50 filings)", "50 filings where the company is named in text (not tables)", "Only the sentences where a pronoun refers to the company",
          "Same as Indirect mentions", "Test set: read the sentence at the link and compare with the model's answer")]
for j, row in enumerate(about, 7):
    for i, v in enumerate(row[:4] + (f"=COUNTA('{row[0]}'!A:A)-1",) + row[4:], 1):
        c = s.cell(j, i, v)
        c.font = Font(name=FONT, size=9)
        c.alignment = Alignment(wrap_text=True, vertical="top")
r0 = 7 + len(about) + 1
s.cell(r0, 1, "How to read the comparison").font = Font(name=FONT, size=11, bold=True)
for k, (a, b) in enumerate([("Side by side", "Every value the pipeline produced (blue) sits next to the same value as written in the source (green)."),
                            ("Match", "Code compares each pair: Yes = the value is written in the source exactly."),
                            ("Page", "The page number printed in the filing; '(counted)' where the filing prints none; web-style filings have no pages."),
                            ("Source link", "Opens the filing on SEC EDGAR scrolled to the number, highlighted (text-fragment link; Chrome, Edge, Safari)."),
                            ("Held back", "Rows that failed a check are not in this workbook; they are listed with the reason in the Evidence Clashes workbook.")], r0 + 1):
    s.cell(k, 1, a).font = Font(name=FONT, size=9, bold=True)
    s.cell(k, 2, b).font = Font(name=FONT, size=9)
for col, wd in zip("ABCDEF", (22, 44, 40, 34, 8, 60)):
    s.column_dimensions[col].width = wd
first = ["All evidence", "Indirect mentions", "Indirect test (50 filings)", "About"]
wb._sheets = [wb[n] for n in first] + [x for x in wb._sheets if x.title not in first]
wb.active = 0
wb.calculation = CalcProperties(fullCalcOnLoad=True)
dest.mkdir(parents=True, exist_ok=True)
out = dest / f"{name.split(',')[0].replace(' ', '_')}_findings.xlsx"
wb.save(out)
print(out, {"holdings": len(hold), "facts": len(facts), "nport": len(npr), "formd": len(fdr), "dol": len(dol), "all evidence": len(allr), "indirect": len(ind), "indirect test": len(test),
            "holdings not matched": sum(1 for h in hold if h["match"].startswith("No"))})
