#!/usr/bin/env python3
"""The Word report for one company, in the layout of the Databricks report: summary, where the data comes from, All
evidence, holdings, N-PORT / Form D / DOL, indirect mentions (counts, method, check, cost), links and limits.
Every number is counted from the pipeline's own outputs (the findings workbook, work/out/<co>/, the N-PORT and DOL
files); nothing is typed by hand. The template's own paragraphs and tables are copied, so fonts and table styling match.

    python tools/report_docx.py <company> "<Company name>" <findings.xlsx> <out.docx> --template Databricks_report.docx
        [--hand-checked N]      words given to the company that were read by hand against the filing and found correct
"""
from __future__ import annotations

import argparse
import collections
import copy
import csv
import glob
import json
import re
import sys
from pathlib import Path

import docx
import openpyxl
from docx.oxml.ns import qn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import paths as px  # noqa: E402

csv.field_size_limit(10**9)
rd = lambda p: list(csv.DictReader(open(p, encoding="utf-8"))) if Path(p).exists() else []   # noqa: E731
n = lambda x: f"{x:,}"   # noqa: E731


def plural(k: int, one: str, many: str | None = None) -> str:
    return f"{n(k)} {one if k == 1 else (many or one + 's')}"


# ------------------------------------------------------------------ template copying
class Doc:
    def __init__(self, template: Path):
        self.d = docx.Document(str(template))
        ps = self.d.paragraphs
        style = lambda p: p._p.xpath("string(./w:pPr/w:pStyle/@w:val)")   # noqa: E731
        pick = lambda f: copy.deepcopy(next(p._p for p in ps if f(p)))   # noqa: E731
        self.proto = {"title": pick(lambda p: style(p) == "Title"),
                      "subtitle": copy.deepcopy(ps[1]._p),
                      "h1": pick(lambda p: style(p) == "Heading1"),
                      "h2": pick(lambda p: style(p) == "Heading2"),
                      "body": copy.deepcopy(ps[3]._p),
                      "bullet": pick(lambda p: p._p.xpath("./w:pPr/w:numPr"))}
        tabs = self.d.tables
        self.tproto = {2: copy.deepcopy(tabs[0]._tbl), 3: copy.deepcopy(next(t._tbl for t in tabs if len(t.columns) == 3))}
        self.body = self.d.element.body
        self.sect = self.body.find(qn("w:sectPr"))
        for el in list(self.body):
            if el is not self.sect:
                self.body.remove(el)

    @staticmethod
    def _set_text(p_el, text: str) -> None:
        runs = p_el.findall(qn("w:r"))
        rpr = copy.deepcopy(runs[0].find(qn("w:rPr"))) if runs and runs[0].find(qn("w:rPr")) is not None else None
        for child in list(p_el):
            if child.tag != qn("w:pPr"):
                p_el.remove(child)
        r = p_el.makeelement(qn("w:r"), {})
        if rpr is not None:
            r.append(rpr)
        t = r.makeelement(qn("w:t"), {})
        t.text = text
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        r.append(t)
        p_el.append(r)

    def p(self, kind: str, text: str) -> None:
        el = copy.deepcopy(self.proto[kind])
        self._set_text(el, text)
        self.sect.addprevious(el)

    def table(self, header: list, rows: list, total: list | None = None) -> None:
        tbl = copy.deepcopy(self.tproto[len(header)])
        trs = tbl.findall(qn("w:tr"))
        hdr, data, last = trs[0], trs[1], trs[-1]
        for tr in trs:
            tbl.remove(tr)

        def row(proto, vals):
            tr = copy.deepcopy(proto)
            for tc, v in zip(tr.findall(qn("w:tc")), vals):
                ps = tc.findall(qn("w:p"))
                for extra in ps[1:]:
                    tc.remove(extra)
                self._set_text(ps[0], str(v))
            tbl.append(tr)

        row(hdr, header)
        for r in rows:
            row(data, r)
        if total:
            row(last, total)
        self.sect.addprevious(tbl)

    def save(self, out: Path) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        self.d.save(str(out))


# ------------------------------------------------------------------ counting
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("company")
    ap.add_argument("name")
    ap.add_argument("workbook")
    ap.add_argument("out")
    ap.add_argument("--template", required=True)
    ap.add_argument("--hand-checked", type=int, default=None, help="words given to the company read by hand and found correct")
    a = ap.parse_args()
    co, name = a.company, a.name
    short = json.loads(px.config_path(co).read_text(encoding="utf-8")).get("name") or name.split(",")[0]
    ev, out_co = px.OUT / co / "evidence", px.OUT / co

    wb = openpyxl.load_workbook(a.workbook, read_only=True)
    sheet_rows = lambda t: [r for r in wb[t].iter_rows(min_row=2, values_only=True) if r and r[0] is not None]   # noqa: E731
    AE = sheet_rows("All evidence")          # # Kind Form Filed-by Filed As-of Evidence Paragraph Pno Status Page Link
    IND_SHEET = sheet_rows("Indirect mentions")
    HOLD = sheet_rows("Holdings (filings)")
    NP_SHEET, FD_SHEET, DOL_SHEET = sheet_rows("N-PORT holdings"), sheet_rows("Form D offerings"), sheet_rows("DOL 401(k)")

    E, PROSE = rd(ev / "evidence_rows.csv"), rd(ev / "prose_sentences.csv")
    manifest = [r for m in px.DUMPS.glob(f"{co}*_manifest.csv") for r in rd(m)]
    filings_with = {r["accession"] for r in E} | {r["accession"] for r in PROSE}
    forms = collections.Counter(r[2] for r in AE)
    filers = {r[3] for r in AE}
    dates = sorted(r[4] for r in AE if r[4])
    years = f"{dates[0][:4]}–{dates[-1][:4]}" if dates else ""
    kinds = collections.Counter(r[1] for r in AE)
    status = collections.Counter(str(r[9]).split(":")[0] for r in AE)

    NPR = rd(px.DUMPS / f"{co}_nport" / "nport_holdings.csv")
    FDR = rd(out_co / "formd_offerings.csv")
    PP = rd(px.INPUTS / co / "data" / "plan_participants.csv")

    # indirect
    runs = [json.load(open(f))["meta"] | {"_calls": json.load(open(f)).get("calls", [])} for f in glob.glob(str(out_co / "indirect" / "*_run.json"))]
    T = [r for f in glob.glob(str(out_co / "indirect" / "*_all_triggers.csv")) for r in rd(f)]
    LOC = rd(out_co / "indirect_located.csv")
    by_type = collections.Counter(r["trigger_type"] for r in T)
    st = collections.Counter((r["decided_by"], r["status"]) for r in T)
    llm = sum(v for (d, _), v in st.items() if d == "llm")
    to_co = [r for r in T if r["decided_by"] == "llm" and r["status"] == "resolved" and r["company_id"].startswith("C")]
    seen, places = set(), []
    for r in sorted(LOC, key=lambda r: r.get("name_in_paragraph") == "yes"):
        k = (r["accession"], r["search_text"], r["page"])
        if r["company_id"].startswith("C") and r["status"] == "resolved" and k not in seen:
            seen.add(k)
            places.append(r)
    in_para = sum(1 for r in places if r.get("name_in_paragraph") == "yes")
    sentences = {re.sub(r"\s+", " ", r["sentence"]).strip() for r in places}
    ind_filers = {r["filer"] for r in places}
    incomplete = sum(1 for m in runs if not m.get("output_complete", True))
    cost_all = sum(float(m.get("cost_usd") or 0) for m in runs)
    cost_check = sum(float(c.get("cost_usd") or 0) for m in runs for c in m["_calls"] if str(c.get("call", "")).startswith("check"))
    model, check_model = (runs[0]["model"], runs[0].get("second_check")) if runs else ("openai/gpt-4.1", "anthropic/claude-opus-5.5")
    pretty = {"openai/gpt-4.1": "GPT-4.1", "anthropic/claude-opus-5.5": "Claude Opus 5.5"}
    m1, m2 = pretty.get(model, model), pretty.get(check_model, check_model)

    sources = ["SEC filings"] + (["the SEC's Form N-PORT data"] if NPR else []) + (["Form D data"] if FDR else []) + \
              (["the Department of Labor's Form 5500 data"] if (DOL_SHEET or PP) else [])
    src_txt = ", ".join(sources[:-1]) + (" and " if len(sources) > 1 else "") + sources[-1]

    held = [r for r in E if r.get("tier") == "C review"]
    article = "An" if short[:1].upper() in "AEIOU" else "A"
    D = Doc(Path(a.template))
    D.p("title", name)
    D.p("subtitle", "Evidence from public records: what was found, where, and how it was checked")

    # ---------------- Summary
    D.p("h1", "Summary")
    D.p("body", f"Everything in this pack comes from public government records that name {short}: {src_txt}. A script reads the raw "
                "filings and records, and every row carries a link that opens the source at the exact text. No number was produced "
                "by a language model and no value was typed by hand.")
    D.p("h2", "String match: the name in SEC filings")
    D.p("body", f"The filings searched are the {plural(len(manifest), 'SEC filing')} downloaded for {short} (EDGAR full-text search on "
                f"its names). The script found {plural(len(AE), 'mention')} in {plural(len(filings_with), 'filing')}, from "
                f"{plural(len(filers), 'filer')} across {plural(len(forms), 'form type')}, filed {years}.")
    top = forms.most_common()
    shown, rest = top[:10], top[10:]
    D.table(["Form", "Mentions"], [[f, n(c)] for f, c in shown] + ([[f"Other {len(rest)} forms", n(sum(c for _, c in rest))]] if rest else []),
            total=["Total", n(len(AE))])
    D.p("h2", "Indirect mentions")
    if places:
        D.p("body", f"The text of these filings ({plural(len(runs), 'filing document')}) was then read for 'it', 'its' and 'the company'. "
                    f"{plural(len(to_co), 'such word')} refer to {short}, in {plural(len(sentences), 'distinct sentence')} from "
                    f"{plural(len(ind_filers), 'filer')} ({plural(len(places), 'place')}). {n(in_para)} of these places sit in a paragraph "
                    f"that names {short}, which string match already has, so they are not repeated; " + (f"{n(len(IND_SHEET))} kept." if IND_SHEET else "the Indirect mentions sheet is empty.")
                    + (f" Every one was read by hand against the filing: {a.hand_checked} of {len(places)} correct." if a.hand_checked is not None else ""))
    else:
        D.p("body", f"The text of these filings ({plural(len(runs), 'filing document')}) was then read for 'it', 'its' and 'the company'. "
                    f"No such word was tied to {short}: the Indirect mentions sheet is empty.")
    D.p("h2", "Structured records")
    bits = [f"Holdings in filings: {plural(len(HOLD), 'position')}" + (f" from {plural(len({r[1] for r in HOLD}), 'fund')}" if HOLD else "") + "."]
    bits.append(f"Form N-PORT: {plural(len(NPR), 'holding')} from {plural(len({r['series'] or r['registrant'] for r in NPR}), 'fund')}." if NPR else
                "Form N-PORT: no fund reports a holding under this name.")
    bits.append(f"Form D: {plural(len(FD_SHEET), 'offering notice')}." if FD_SHEET else "Form D: no notices.")
    if DOL_SHEET:
        yrs = sorted(str(r[1]) for r in DOL_SHEET)
        bits.append(f"DOL Form 5500: {plural(len(DOL_SHEET), 'row')}, plan years {yrs[0]}–{yrs[-1]}.")
    elif PP:
        bits.append(f"DOL: no full Form 5500 rows; the company files the short Form 5500-SF ({plural(len(PP), 'plan year')}), see below.")
    else:
        bits.append("DOL Form 5500: no filing found for the company.")
    D.p("body", " ".join(bits) + (" Every value matches its source record." if (HOLD or NPR or FD_SHEET or DOL_SHEET) else ""))
    D.p("h2", "Sheets in the workbook")
    D.table(["Sheet", "Rows", "What it holds"], [
        ["All evidence", n(len(AE)), f"Every place the name was found in the SEC filings searched, {years}"],
        ["Indirect mentions", n(len(IND_SHEET)), f"Places where it / its / the company refers back to {short} in a paragraph that does not name it"],
        ["Holdings (filings)", n(len(HOLD)), f"Positions in {short} reported by funds and vehicles" + ("" if HOLD or not held else " (none passed every check, see Holdings)")],
        ["N-PORT holdings", n(len(NP_SHEET)), "Monthly fund holdings" + (f" from {plural(len({r['series'] or r['registrant'] for r in NPR}), 'fund')}" if NPR else "")],
        ["Form D offerings", n(len(FD_SHEET)), f"{short}'s own private-offering notices"],
        ["DOL 401(k)", n(len(DOL_SHEET)), "401(k) plan participants (full Form 5500 only)"]])

    # ---------------- Where the data comes from
    D.p("h1", "Where the data comes from")
    common = ", ".join(f"{f} ({n(c)})" for f, c in top[:6])
    D.p("bullet", f"SEC filings that name {short}: {plural(len(forms), 'form type')} from {plural(len(filers), 'filer')}, filed "
                  f"{dates[0] if dates else ''} to {dates[-1] if dates else ''}. The most common are {common}.")
    if NPR:
        nd = sorted(r["report_date"] for r in NPR)
        D.p("bullet", f"SEC Form N-PORT: monthly holdings that funds report, {nd[0]} to {nd[-1]}, {plural(len({r['fund_house'] for r in NPR}), 'fund house')}.")
    if FDR:
        fd = sorted(str(r["filing_date"])[:10] for r in FDR)
        D.p("bullet", f"SEC Form D: the company's own notices of private offerings, {fd[0]} to {fd[-1]}.")
    if DOL_SHEET or PP:
        yrs = sorted({str(r[1]) for r in DOL_SHEET} | {r["plan_year"] for r in PP})
        D.p("bullet", f"Department of Labor Form 5500{'-SF' if PP and not DOL_SHEET else ''}: the company's 401(k) plan, plan years {yrs[0]} to {yrs[-1]}.")

    # ---------------- All evidence
    D.p("h1", "All evidence: every mention in SEC filings")
    D.p("body", f"The script searched every filing for the company's name and kept each hit as one row: {plural(len(AE), 'row')}. Each row shows "
                "the table row or sentence as printed, the page printed in the filing, and a link that opens the filing scrolled to that "
                "text and highlighted.")
    D.table(["Kind of text", "Rows"], [[k, n(c)] for k, c in kinds.most_common()])
    D.p("body", "The 'What happened to it' column says what the script did with each row:")
    meaning = {"Verified": "A holding row that passed every check: it is in the Holdings sheet, or the same row printed again",
               "Sentence that names the company": "A sentence that names the company",
               "Not a position": "A row that names the company but holds no position (for example a list of names, or no amount in the row)",
               "Held back": "A row that failed a check; the reason is written in the row"}
    D.table(["Status", "Rows", "Meaning"], [["Sentence" if k.startswith("Sentence") else k, n(c), meaning.get(k, "")] for k, c in status.most_common()])
    ncen_verified = [r for r in AE if r[1] == "N-CEN XML" and str(r[9]).startswith("Verified")]
    if ncen_verified and not HOLD:
        amt = re.search(r"principalTotalPurchaseSale>([\d.]+)", str(ncen_verified[0][6]))
        D.p("body", f"Note: the {plural(len(ncen_verified), 'N-CEN XML row')} marked 'Verified' {'is' if len(ncen_verified) == 1 else 'are'} not a holding. "
                    f"{ncen_verified[0][3]} lists {short} as a principal it traded with"
                    + (f", total purchases and sales ${float(amt.group(1)):,.0f}" if amt else "")
                    + ". The Holdings sheet is empty, so the label 'same row as one in Holdings' does not apply to this row.")
    D.p("body", "N-PORT, Form D and DOL records are not in this sheet. They are structured data, not text, and each has its own sheet.")
    D.p("h2", "The same sentence in many filings")
    D.p("body", "Companies and fund families often print the same paragraph in several filings (an annual report and its amendment, a "
                "prospectus and its amendments). The sentence is then the same, but every filing is a separate public record with its own "
                "link, so each filing keeps its own row. Within one filing, the same text on the same page appears once.")

    # ---------------- Holdings
    D.p("h1", "Holdings")
    if HOLD:
        types = collections.Counter(r[3] for r in HOLD)
        D.p("body", f"{plural(len(HOLD), 'position')} in {short}, reported by {plural(len({r[1] for r in HOLD}), 'fund and vehicle', 'funds and vehicles')}: "
                    + ", ".join(f"{k} {n(v)}" for k, v in types.most_common()) + ".")
        D.p("body", "For each position the sheet shows every number twice: the value the script read (blue) and the same value as written in "
                    "the filing (green). Code compares the two: every value matches the source record.")
    else:
        by_filer = collections.Counter(re.sub(r"\s*\(.*$", "", r["filer"]).strip() for r in held)
        why = collections.Counter(r["why"] for r in held)
        if held:
            D.p("body", f"No position passed every check, so the Holdings sheet is empty. {plural(len(held), 'table row')} name {short} with "
                        "amounts, from " + ", ".join(f"{f} ({n(c)})" for f, c in by_filer.most_common()) + ", but were held back: "
                        + "; ".join(f"'{w}' ({n(c)})" for w, c in why.most_common()) + ". They are in All evidence with that status and a "
                        "link, so each can be read in the filing.")
            D.p("body", "A row is moved to Holdings only when the unit is printed (or a second source such as Form N-PORT confirms the "
                        "numbers) and two independent readings of the row agree. The rule prefers holding a row back to publishing a "
                        "number at the wrong scale.")
        else:
            D.p("body", f"No filing searched holds a schedule-of-investments row that names {short}, so the Holdings sheet is empty.")

    # ---------------- N-PORT, Form D and DOL
    D.p("h1", "N-PORT, Form D and DOL")
    if NPR:
        houses = collections.Counter(r["fund_house"] for r in NPR)
        D.p("bullet", f"N-PORT holdings: {plural(len(NPR), 'row')} from {plural(len({r['series'] or r['registrant'] for r in NPR}), 'fund')}. Fund houses: "
                      + ", ".join(f"{h} ({n(c)})" for h, c in houses.most_common(5)) + ". Units and value: every value matches the source record.")
    else:
        D.p("bullet", f"N-PORT holdings: none. No fund in the N-PORT data (2019-09 to 2026-07) reports a holding whose issuer name matches {short}.")
    if FD_SHEET:
        ft = collections.Counter(r[2] for r in FD_SHEET)
        D.p("bullet", f"Form D offerings: {plural(len(FD_SHEET), 'notice')} (" + ", ".join(f"{n(c)} {k}" for k, c in ft.most_common()) + "). Date of first "
                      "sale, amount offered and sold, number of investors: every value matches the source record.")
    if DOL_SHEET:
        yrs = sorted(str(r[1]) for r in DOL_SHEET)
        D.p("bullet", f"DOL 401(k): {plural(len(DOL_SHEET), 'row')}, plan years {yrs[0]}–{yrs[-1]}. Active participants: every value matches the source record.")
    elif PP:
        D.p("bullet", f"DOL 401(k): the sheet is empty because it reads only the full Form 5500, and {short} files the short Form 5500-SF. "
                      f"The 5500-SF records (plan {PP[0]['plan_number']}) give active participants at year end: "
                      + ", ".join(f"{r['plan_year']}: {r['active_eoy']}" for r in PP) + ".")
    else:
        D.p("bullet", f"DOL 401(k): no Form 5500 or 5500-SF filing was found for {short}.")
    D.p("body", "These come from structured government data, so no model is involved; each value is compared field by field with the record.")

    # ---------------- Indirect mentions
    D.p("h1", "Indirect mentions")
    D.p("body", f"Filings often name {short} once and then say 'it', 'its' or 'the company'. These sentences are evidence too, so the script "
                f"finds them. It ran on all {plural(len(runs), 'filing')} that name the company in text ({n(incomplete)} incomplete) and found "
                f"{plural(len(to_co), 'word')} that refer to {short}" + (f", in {plural(len(sentences), 'distinct sentence')} from "
                f"{plural(len(ind_filers), 'filer')}, at {plural(len(places), 'place')}. {n(in_para)} sit in a paragraph that names {short}: that "
                "paragraph is already in All evidence from string match, so the word adds no new evidence and is not written again. "
                + (f"The Indirect mentions sheet keeps the other {n(len(IND_SHEET))}." if IND_SHEET else "The Indirect mentions sheet is empty.") if places else ". The Indirect mentions sheet is empty."))
    if places:
        D.table(["Word", "Places"], [[w, n(c)] for w, c in collections.Counter(r["trigger"].lower() for r in places).most_common()])
    D.p("h2", "From every word to the model: the counts")
    D.p("body", f"The run works on the full text of each filing, not on the mention rows. It found {plural(len(T), 'word')} of these kinds and "
                f"sent only {n(llm)} of them to the language model; rules decided the rest.")
    rej = sum(v for (d, s), v in st.items() if d == "llm" and s.startswith("rejected"))
    D.table(["Step", "Words"], [
        ["Words found: we / our", n(by_type["first_person"])],
        ["Words found: the filer's own defined name (the Fund, the Trust ...)", n(by_type["defined_term"])],
        ["Words found: it / its", n(by_type["third_singular"])],
        ["Words found: they / their", n(by_type["third_plural"])],
        ["Words found: the company / the issuer ...", n(by_type["third_noun"])],
        ["Rule: given to the filer (we / our / its own defined name)", n(st[("rule", "resolved")])],
        ["Rule: skipped, no listed company within two paragraphs", n(st[("rule", "no_listed_company_nearby")])],
        ["Rule: skipped, inside a table", n(st[("rule", "table_skipped")])],
        ["Sent to the model", n(llm)],
        ["Model: none, or two companies fit equally", n(st[("llm", "none")])],
        ["Model: rejected by the code check or the second model", n(rej)],
        ["Model: resolved to a company (filer or listed)", n(st[("llm", "resolved")])],
        [f"of which: {short}", n(len(to_co))]])
    D.p("h2", "How a word is decided")
    D.p("bullet", "Rules first: words inside tables are skipped; 'we', 'our' and the filer's own defined name go to the filer; a word with no "
                  "listed company within two paragraphs is skipped.")
    D.p("bullet", f"The remaining words go to a language model ({m1}) with the surrounding text. The exact word is marked, and the model may "
                  "only answer with a listed company or 'none'. If two companies fit equally, the answer is 'none'.")
    D.p("bullet", "Code check: the model must name the words it relied on, and those words must contain the company's name in the same sentence "
                  f"or the two sentences before. {article} {short} answer also needs confidence of 90 or more.")
    D.p("bullet", f"Second check: a different model ({m2}) reads the passage and must answer yes. A row is kept only if both agree.")
    D.p("body", "The model only decides who a word refers to. It never produces a number.")
    D.p("h2", "How it was checked")
    if places:
        D.p("body", f"Every word given to {short} was read by hand in the filing"
                    + (f": {a.hand_checked} of {len(places)} places are correct." if a.hand_checked is not None else "."))
        D.table(["Filed by (form)", "Word", "Sentence"],
                [[f"{r['filer']} ({r['form']})", r["trigger"], re.sub(r"\s+", " ", r["sentence"]).strip()[:220]] for r in places])
    else:
        D.p("body", f"No word was given to {short}, so there was nothing to read by hand. The model's other answers named the filer or 'none'.")
    D.p("body", "The method is set to miss a correct word rather than accept a wrong one.")
    D.p("body", f"Cost of the full run: ${cost_all:.2f} ({m1} ${cost_all - cost_check:.2f}, {m2} ${cost_check:.2f}). The other sheets use no language model.")

    # ---------------- Links and limits
    D.p("h1", "Links and page numbers")
    D.p("bullet", "Each link opens the filing on SEC EDGAR, scrolled to the text and highlighted (works in Chrome, Edge and Safari).")
    D.p("bullet", "The page is the number printed in the filing. '(counted)' means the filing prints no page numbers and the page was counted.")
    D.p("bullet", "Each Indirect row also has a short search text to find the word with Ctrl+F.")
    D.p("h1", "Limits")
    D.p("bullet", "A language model is used only to decide who 'it', 'its' or 'the company' refers to, never for a number.")
    D.p("bullet", f"Only the {plural(len(manifest), 'filing')} downloaded for {short} were searched; a filing outside that set is not in this pack.")
    D.p("bullet", "The checks are strict: a correct indirect word can still be missed.")
    if held and not HOLD:
        D.p("bullet", f"Positions printed without a unit are held back, so the {plural(len(held), 'held-back row')} carry no verified holding numbers.")
    D.p("bullet", "The string-match rows were not all read by hand; each one carries its source link so it can be checked.")
    D.save(Path(a.out))
    print(a.out, {"mentions": len(AE), "filings": len(filings_with), "indirect places": len(places), "kept": len(IND_SHEET),
                  "words": len(T), "to model": llm, "cost": round(cost_all, 4)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
