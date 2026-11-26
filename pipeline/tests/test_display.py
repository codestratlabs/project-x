"""How table rows, list items and PDF leader rows are shown in the workbook. Cases cut from real filings:
Frontier Funds 10-Q 0001213900-24-041845 (a sign in its own cell), Frontier 8-K 0001213900-24-011066 (a bullet laid
out as a one-row table), UBS 20-F 0001610520-26-000023 (a list drawn as boxes), Royal Caribbean 10-K 0000884887-05-000004
(a dot-leader table in a PDF, "Horizon" with footnote mark 3).

    python pipeline/tests/test_display.py
"""
import ast
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "evidence"))
sys.path.insert(0, str(HERE))
import prose_reader as P  # noqa: E402
import evidence_tables as E  # noqa: E402

FAILS = []


def check(name, got, want):
    ok = got == want
    print("OK " if ok else "BAD", name, "" if ok else f"\n    got  {got!r}\n    want {want!r}")
    if not ok:
        FAILS.append(name)


# findings_excel.py is a script (it builds the workbook when run): take only its display helpers
src = (HERE / "findings_excel.py").read_text(encoding="utf-8")
ns = {"re": re}
for node in ast.parse(src).body:
    if isinstance(node, ast.FunctionDef) and node.name in ("tidy", "table_text", "list_item", "lookalike_text") or \
       isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "BULLET" for t in node.targets):
        exec(compile(ast.Module([node], []), "findings_excel.py", "exec"), ns)
T, L = ns["table_text"], ns["list_item"]

# 1. signs printed in their own cell go back on their number; nothing else changes
check("bracket cell", T("Galaxy Plus Fund - Horizon3 Feeder Fund (577) LLC | ( 2,524 | ) | ( 984 | ) | - | -"),
      "Galaxy Plus Fund - Horizon3 Feeder Fund (577) LLC | (2,524) | (984) | - | -")
check("percent cell", T("Horizon3 Investment Management LLP | - | - | - | 13 | % | - | - | -"),
      "Horizon3 Investment Management LLP | - | - | - | 13% | - | - | -")
check("dollar cell", T("Lungpacer Medical Inc. | $ | 1,250,000 | $ | 980,000"), "Lungpacer Medical Inc. | $1,250,000 | $980,000")
check("bracket and dollar", T("X | ($ | 12,000 | )"), "X | ($12,000)")
check("ordinary row kept", T("Lungpacer Medical, Inc. | Series B | 3/1/20 | 1,000 | 2,000"), "Lungpacer Medical, Inc. | Series B | 3/1/20 | 1,000 | 2,000")
check("text in brackets kept", T("Lungpacer (a) | Preferred | 500"), "Lungpacer (a) | Preferred | 500")
check("dash cells kept", T("A | - | – | —"), "A | - | – | —")

# 2. a bullet laid out as a one-row table is a list item; a data row is not
check("bullet row", L({"method": "table", "raw": "● | Horizon3 Investment Management LLP"}), True)
check("data row", L({"method": "table", "raw": "Horizon3 Investment Management LLP | Systematic | Trading Company"}), False)
check("two cells with a number", L({"method": "table", "raw": "• | Lungpacer 1,000 shares"}), False)
check("dash and number cells", L({"method": "table", "raw": "Horizon3 | -"}), False)
check("not a table", L({"method": "iXBRL tag", "raw": "● | x"}), False)

# 3. a line after a lone dash box is a list item; an ordinary sentence is still a sentence
ubs = ("<div style='position:absolute'>Other activities and functions</div><div>&#8211;</div>"
       "<div>Member of the Board of Backflip AI, Inc. </div><div>&#8211;</div><div>Member of the Board of Horizon 3 AI, Inc. </div>")
d = P.Doc(ubs)
p = ubs.index("Horizon 3 AI")
check("UBS dash list line", d.mention(p, p + len("Horizon 3 AI"))["kind"], "list item")
prose = "<p>The company signed an agreement.</p><p>Horizon 3 AI, Inc. raised money from investors in its Series D round.</p>"
d = P.Doc(prose)
p = prose.index("Horizon 3 AI")
check("ordinary sentence", d.mention(p, p + len("Horizon 3 AI"))["kind"], "sentence")
li = "<p>&#8211;</p><p>Horizon 3 AI, Inc. raised money from investors in its Series D round and then it grew.</p>"
d = P.Doc(li)
p = li.index("Horizon 3 AI")
check("long sentence after a dash", d.mention(p, p + len("Horizon 3 AI"))["kind"], "list item")

# 4. a PDF dot-leader row: that one row, not the rows around it
lines = ["Galaxy ......................... 1996 1,870 Panama Canal, Europe",
         "Zenith.......................... 1992 1,374 Caribbean, Bermuda",
         "Horizon3",
         "........................ 1990 1,354 Bermuda, Caribbean",
         "Xpedition4 .................... 2004 100 Galapagos Islands", "1"]
check("leader on next line", bool(E.LEADER.match(lines[3])), True)
check("no leader in prose", bool(E.LEADER.search("The Company sold 1,000 shares. It then raised money.")), False)
check("ellipsis is not a leader", bool(E.LEADER.search("He said ... and left.")), False)

# 5. a sentence's date is the filing's own period (its iXBRL cover tag), not the table heading above it
q = ('<ix:nonNumeric name="dei:DocumentPeriodEndDate" contextRef="c1">September&#160;30, 2024</ix:nonNumeric>'
     '<p>Consolidated Schedule of Investments December 31, 2023</p><p>Horizon3 finished negative for the quarter.</p>')
check("10-Q period from cover tag", E.doc_period(q), "September 30, 2024")
nested = ('<ix:nonNumeric contextRef="c0" name="dei:DocumentPeriodEndDate" id="ixv-1">March 31, <ix:nonNumeric contextRef="c0" '
          'name="dei:DocumentFiscalYearFocus" id="ixv-2">2024</ix:nonNumeric></ix:nonNumeric>')
check("date split by a nested tag (Frontier 10-Q)", E.doc_period(nested), "March 31, 2024")
fy = ('<ix:nonNumeric name="dei:DocumentPeriodEndDate"><ix:nonNumeric format="ixt:date-monthname-day-en" '
      'name="dei:CurrentFiscalYearEndDate">December 31</ix:nonNumeric>, <ix:nonNumeric name="dei:DocumentFiscalYearFocus">2024</ix:nonNumeric></ix:nonNumeric>')
check("date built from two nested tags (Frontier 10-K)", E.doc_period(fy), "December 31, 2024")
check("no cover tag", E.doc_period("<p>Schedule of Investments December 31, 2023</p>"), "")
two = ('<ix:nonNumeric name="dei:DocumentPeriodEndDate">2024-09-30</ix:nonNumeric>'
       '<ix:nonNumeric name="dei:DocumentPeriodEndDate">2024-06-30</ix:nonNumeric>')
check("two different periods", E.doc_period(two), "")

# 6. a sentence takes the period its own text names: last year's paragraph reprinted in a 10-Q (Frontier 10-Q 0001213900-25-044045)
def named(html_, period, which=0):
    d = P.Doc(html_)
    p = [m.start() for m in re.finditer("Horizon3", html_)][which]
    x = d.mention(p, p + len("Horizon3"))
    return d.named_period(p, x["sentence"], period)
fq = ("<p><b>Frontier Balanced Fund</b></p><p><i>2025</i></p>"
      "<p>One of the six sectors which traded in the Frontier Balanced Fund was profitable in Q1 2025 and one of the six was profitable YTD.</p>"
      "<p>In terms of major CTA performance, QIM finished positive for the quarter while Aspect, Fort, Horizon3 and Welton were negative for the quarter.</p>"
      "<p><i>2024</i></p>"
      "<p>Four of the six sectors which traded in the Frontier Balanced Fund were profitable in Q1 2024 and four of the six were profitable YTD.</p>"
      "<p>In terms of major CTA performance, Aspect, Fort, Quantica, QIM and Welton finished positive for the quarter while Horizon3 finished negative for the quarter.</p>")
check("this year's paragraph keeps the filing's period", named(fq, "March 31, 2025", 0), "")
check("last year's paragraph: Q1 2024", named(fq, "March 31, 2025", 1), "March 31, 2024")
check("same, period written as YYYY-MM-DD", named(fq, "2025-03-31", 1), "2024-03-31")
fk = ("<p><b>Sector &amp; Major Advisor Attribution for the Frontier Balanced Fund</b></p>"
      "<p>One of the six sectors traded in the Frontier Balanced Fund was profitable in Q4 2023. Stock Indices were positive for the quarter.</p>"
      "<p>Agriculturals were positive YTD while Currencies, Energies, Interest Rates, Metals and Stock Indices were negative YTD.</p>"
      "<p>In terms of major CTA performance, no CTAs finished positive for the quarter. Aspect, Fort, Horizon3, Quantica, Quest, QIM and Welton finished negative for the quarter.</p>")
check("period named two paragraphs up (10-K, Q4 2023)", named(fk, "December 31, 2024"), "December 31, 2023")
check("quarter that is not the filing's quarter: not dated", named(fk, "June 30, 2024"), "")
hd = ("<p>Four of the six sectors which traded in the fund were profitable in Q1 2023 and four of the six were profitable YTD.</p>"
      "<p><b>Results of Operations</b></p><p>In terms of major CTA performance, Horizon3 finished negative for the quarter while others were positive.</p>")
check("a heading ends the search", named(hd, "March 31, 2024"), "")
two = ("<p>For the three months ended March 31, 2024 and 2023, the fund recorded losses on many of its trades in every sector.</p>"
       "<p>In terms of major CTA performance, Horizon3 finished negative for the quarter while others were positive.</p>")
check("two periods named: filing's period kept", named(two, "March 31, 2024"), "")
ex = ("<p>For the three months ended March 31, 2023, the Frontier Balanced Fund recorded total expenses of $147,302, net of fees.</p>"
      "<p>In terms of major CTA performance, no CTAs finished positive for the quarter while Horizon3 finished negative for the quarter.</p>")
check("exact period from 'three months ended'", named(ex, "March 31, 2024"), "March 31, 2023")
old = ("<p>Four of the six sectors which traded in the fund were profitable in Q1 2019 and four of the six were profitable YTD.</p>"
       "<p>In terms of major CTA performance, Horizon3 finished negative for the quarter while others were positive.</p>")
check("more than two years back: not used", named(old, "March 31, 2025"), "")
cur = ("<p>Effective January 11, 2024, Horizon3 Investment Management LLP was added as a major commodity trading advisor for the fund.</p>")
check("a plain date is not a period", named(cur, "December 31, 2024"), "")

# 7. the one "as of" date printed for a whole table (Frontier 8-K 0001213900-25-118400, 10-Q 0001213900-23-039607)
def tdate(h):
    return E.printed_table_date(h, h.rfind("<table", 0, h.index("Horizon3")), h.index("Horizon3"))   # the table that holds the name, as the scan finds it
k8 = ('<p>As of December 1, 2025, the allocation of the assets of the Frontier Balanced Fund between the trading advisors was as follows:</p><p>&#160;</p>'
      '<table><tr><td><b>Advisor</b></td><td>&#160;</td><td><b>Allocation as of December 1, 2025<br/></b>(expressed as a percentage of aggregate notional <br/>'
      'exposure to commodity trading programs)</td></tr><tr><td>Aspect Capital Limited</td><td>&#160;</td><td>0%</td></tr>'
      '<tr><td>Horizon3 Investment Management LLP</td><td>&#160;</td><td>0%</td></tr></table>')
check("8-K: header cell and lead-in", tdate(k8), "December 1, 2025")
q5 = ('<p>As of March 31, 2023, the allocation of the assets of each applicable Series of the Trust among the Trading Advisors was as follows:</p>'
      '<p><b>Allocation as of March 31, 2023</b> (expressed as a percentage of aggregate notional exposure to commodity trading programs)</p>'
      '<table><tr><td>Advisor</td><td>Frontier Balanced Fund</td></tr><tr><td>Aspect Capital Limited</td><td>23%</td></tr>'
      '<tr><td>Horizon3 Investment Management LLP</td><td>13%</td></tr></table>')
check("10-Q: heading and lead-in above the table", tdate(q5), "March 31, 2023")
q4 = ('<p>A multi-manager approach to portfolio management provides diversification of Trading Advisors and access to broader global markets.</p>'
      '<p>As of March 31, 2023, the trading system of each of the major Trading Advisors and the means by which the Series access those Trading Advisors were as follows:</p>'
      '<p>&#160;</p><table><tr><td>Major Commodity Trading Advisor</td><td>Trading System Style</td></tr>'
      '<tr><td>Horizon3 Investment Management LLP</td><td>Systematic</td></tr></table>')
check("10-Q: lead-in ending in ':'", tdate(q4), "March 31, 2023")
t2 = ('<p>As of March 31, 2024 and December 31, 2023, the allocation was as follows:</p>'
      '<table><tr><td>Advisor</td><td>A</td></tr><tr><td>Horizon3 Investment Management LLP</td><td>13%</td></tr></table>')
check("two periods in the lead-in: none", tdate(t2), "")
para = ('<p>' + "The fund trades futures across many markets and sectors and its returns vary from quarter to quarter " * 3 + 'as of March 31, 2023.</p>'
        '<table><tr><td>Advisor</td><td>A</td></tr><tr><td>Horizon3 Investment Management LLP</td><td>13%</td></tr></table>')
check("an ordinary paragraph above: none", tdate(para), "")
prev = ('<table><tr><td>Allocation as of June 30, 2024</td></tr><tr><td>X</td><td>5%</td></tr></table>'
        '<table><tr><td>Advisor</td><td>A</td></tr><tr><td>Horizon3 Investment Management LLP</td><td>13%</td></tr></table>')
check("the table above's date is not used", tdate(prev), "")

# 8. a sentence is listed once per paragraph: the same words printed again in another paragraph are a mention of their own
import tempfile, shutil
L = P.listed
seen, raw_seen = set(), set()
check("first sentence", L(seen, raw_seen, "u", "S", "sentence", 100, "S"), True)
check("same sentence, same paragraph (name twice)", L(seen, raw_seen, "u", "S", "sentence", 100, "S"), False)
check("same words, another paragraph (reprint)", L(seen, raw_seen, "u", "S", "sentence", 900, "S"), True)
check("same words, another filing", L(seen, raw_seen, "v", "S", "sentence", 100, "S"), True)
check("hidden data: once per text, as before", L(seen, raw_seen, "u", "H", "hidden", 5, ""), True)
check("hidden data, same text again", L(seen, raw_seen, "u", "H", "hidden", 50, ""), False)
check("heading: once per text, as before", L(seen, raw_seen, "u", "G", "heading or label", 7, "G"), True)
check("heading, same text elsewhere", L(seen, raw_seen, "u", "G", "heading or label", 70, "G"), False)
pb = ("<p>The board said Horizon3 AI, Inc. signed the</p><hr style='page-break-after:always'/><p>3</p><hr style='page-break-after:always'/>"
      "<p>agreement with Horizon3 Federal in March of that year.</p>")
d = P.Doc(pb)
p1, p2 = pb.index("Horizon3"), pb.rindex("Horizon3")
check("sentence across a page break: both halves one paragraph", d.para_start(p1) == d.para_start(p2), True)
check("... and one sentence", d.mention(p1, p1 + 8)["sentence"] == d.mention(p2, p2 + 8)["sentence"], True)
tmp = Path(tempfile.mkdtemp(dir=str(Path(__file__).resolve().parent)))
try:
    f = tmp / "q.htm"
    f.write_text("<html><body>"
                 "<p>In terms of major CTA performance, no CTAs finished positive for the quarter while Horizon3 finished negative for the quarter.</p>"
                 "<p>Markets were mixed during the period and many sectors lost money for the funds.</p>"
                 "<p>In terms of major CTA performance, no CTAs finished positive for the quarter while Horizon3 finished negative for the quarter.</p>"
                 "<p>The board of Horizon3 met the board of Horizon3 Federal in March.</p></body></html>", encoding="utf-8")
    res = E.scan((str(f), "horizon ?3", str(tmp), "10-Q", "0000000000-25-000001", "2025-08-14"))
    check("scan: reprint in another paragraph kept, name twice in one sentence once", len(res.get("prose", [])), 3)
finally:
    shutil.rmtree(tmp)

# 9. the look-alike reason: no count; its example only when the filing prints it as one phrase
why = "look-alike name: company-finder judged every mention in this filing to be another company"
LT = ns["lookalike_text"]
check("example stitched from two PDF lines: left out", LT(why + " (e.g. Bermuda Horizon3)", ["Horizon3 ........ 1990 1,354 Bermuda, Caribbean"]), why)
check("example printed in a row: kept", LT(why + " (e.g. Horizon3 Investment Management LLP)", ["Horizon3 Investment Management LLP | Systematic | Trading Company"]),
      why + " (e.g. Horizon3 Investment Management LLP)")
check("spacing and case do not matter", LT(why + " (e.g. Time horizon3)", ["… Value chain step Time  Horizon3 Type …"]), why + " (e.g. Time horizon3)")
check("no example: unchanged", LT(why, []), why)

# 10. link text that lands on its own row (fragments.py)
import fragments as FR
def own_of(h, nth, word="Horizon3"):
    return ("pos", [m.start() for m in re.finditer(word, h)][nth])
def ol(pg, own, st, prefix=""):
    r = FR.own_link(pg, own, st, prefix)
    return (r["mode"], r["start"], r["end"])
rep = ("<p>One of the six sectors was profitable in Q2 2025.</p>"
       "<p>In terms of major CTA performance, no CTAs finished positive for the quarter while Horizon3, Quantica and Welton finished negative for the quarter. No CTAs finished positive YTD.</p>"
       "<p>Three of the six sectors were profitable in Q2 2024.</p>"
       "<p>In terms of major CTA performance, no CTAs finished positive for the quarter while Horizon3, Quantica and Welton finished negative for the quarter. Quantica finished positive YTD.</p>")
pg = FR.Page(rep)
st = "the quarter while Horizon3, Quantica and"
check("reprint: the first copy keeps its link", ol(pg, own_of(rep, 0), st), ("keep", "", ""))
check("reprint: the second copy gets the shortest text printed only there", ol(pg, own_of(rep, 1), st), ("text", st + " Welton finished negative for the quarter. Quantica", ""))
tabs = ("<table><tr><td>Advisor</td><td>Fund</td><td>Share</td></tr><tr><td>Horizon3 Investment Management LLP</td><td>Balanced</td><td>13%</td></tr></table>"
        "<table><tr><td>Advisor</td><td>Fund</td><td>Share</td></tr><tr><td>Horizon3 Investment Management LLP</td><td>Select</td><td>13%</td></tr></table>")
pg = FR.Page(tabs)
trs = [m for m in re.finditer(r"<tr\b[^>]*>.*?</tr>", tabs)]
check("table: same name and number in two tables -> another cell of the row", ol(pg, ("row", trs[3].start(), trs[3].end()), "13"), ("text", "Select", ""))
check("table: the first table's row keeps its link", ol(pg, ("row", trs[1].start(), trs[1].end()), "13"), ("keep", "", ""))
heads = "<p><b>Galaxy Plus Fund – Horizon3 Master Fund (577) LLC</b></p><p>Statement of Financial Condition</p>" * 3
pg = FR.Page(heads)
check("same heading on three pages: the first keeps its link", ol(pg, own_of(heads, 0), "Plus Fund – Horizon3 Master Fund")[0], "keep")
check("same heading, nothing unique near it: a later copy opens the filing", ol(pg, own_of(heads, 2), "Plus Fund – Horizon3 Master Fund"), ("plain", "", ""))
red = "".join(f"<p>The following table summarizes the redemption terms for the {f} Fund as of March 31, 2024:</p>"
              "<table><tr><td>Investment</td><td>Notice</td></tr><tr><td>Galaxy Plus Fund - Horizon3 Feeder Fund (577) LLC</td><td>24 hours</td></tr></table>"
              for f in ("Balanced", "Select", "Heritage"))
pg = FR.Page(red)
trs = [m for m in re.finditer(r"<tr\b[^>]*>.*?</tr>", red)]
check("identical rows: the first keeps its link", ol(pg, ("row", trs[1].start(), trs[1].end()), "Galaxy Plus Fund - Horizon3 Feeder Fund")[0], "keep")
check("identical rows: a later one searches its table's own lead-in, then the row",
      ol(pg, ("row", trs[3].start(), trs[3].end()), "Galaxy Plus Fund - Horizon3 Feeder Fund"), ("range", "Select Fund as of March 31, 2024:", "Galaxy Plus Fund - Horizon3 Feeder Fund"))
betw = ("<p>The only lead-in line printed once here:</p><table><tr><td>Galaxy Plus Fund - Horizon3 Feeder Fund</td></tr><tr><td>Galaxy Plus Fund - Horizon3 Feeder Fund</td></tr></table>"
        "<p>Galaxy Plus Fund - Horizon3 Feeder Fund</p>")
pg = FR.Page(betw)
trs = [m for m in re.finditer(r"<tr\b[^>]*>.*?</tr>", betw)]
check("another copy between the start and the row: no range (plain)", ol(pg, ("row", trs[1].start(), trs[1].end()), "Galaxy Plus Fund - Horizon3 Feeder Fund")[0], "plain")
yr = "<p>In 2014 the fund began trading.</p><table><tr><td>Horizon3</td><td>14%</td></tr></table>"
pg = FR.Page(yr)
check("'14' inside '2014' first: the cell's whole '14%' is used", ol(pg, ("row", yr.index("<tr"), yr.index("</tr>") + 5), "14"), ("text", "14%", ""))
hid = "<div style='display:none'>Horizon3 Investment Management LLP</div><p>Horizon3 Investment Management LLP was added as an advisor.</p>"
pg = FR.Page(hid)
check("a hidden copy is not searched by a browser", FR.own_link(pg, own_of(hid, 1), "Horizon3 Investment Management LLP")["check"], "lands on its row: printed only here")
src = (HERE / "findings_excel.py").read_text(encoding="utf-8")
ln = {}
for node in ast.parse(src).body:
    if isinstance(node, ast.FunctionDef) and node.name in ("link", "enc", "row_link") or isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "enc" for t in node.targets):
        exec(compile(ast.Module([node], []), "findings_excel.py", "exec"), {**ln, "re": re, "urllib": __import__("urllib.parse")}, ln)
u = "https://www.sec.gov/x/f.htm"
check("row_link keep = the old link exactly", ln["row_link"](u, {"link_mode": "keep"}, "14", "Fund"), ln["link"](u, "14", "Fund"))
check("row_link with an older locator (no mode) = the old link", ln["row_link"](u, {}, "the quarter while Horizon3", ""), ln["link"](u, "the quarter while Horizon3", ""))
check("row_link text", ln["row_link"](u, {"link_mode": "text", "link_start": "Select"}, "13"), u + "#:~:text=Select")
check("row_link range (the dash encoded)", ln["row_link"](u, {"link_mode": "range", "link_start": "Select Fund:", "link_end": "Galaxy Plus Fund - Horizon3"}, "x"),
      u + "#:~:text=Select%20Fund%3A,Galaxy%20Plus%20Fund%20%2D%20Horizon3")
check("row_link plain", ln["row_link"](u, {"link_mode": "plain"}, "x"), u)
check("row_link: a PDF keeps its plain file link", ln["row_link"]("https://www.sec.gov/x/f.pdf", {"link_mode": "range", "link_start": "a", "link_end": "b"}, "x"), "https://www.sec.gov/x/f.pdf")
for x in ("( 2,524 )", "Horizon3 Investment Management LLP , Inc .", "a  b", "13 %"):
    want = ln["link"]("https://x/y.htm", x).split("#:~:text=", 1)[1]
    check(f"browser_text cleans like findings_excel.link: {x!r}", ln["link"]("https://x/y.htm", FR.browser_text(x)).split("#:~:text=", 1)[1], want)

print("failures:", len(FAILS))
sys.exit(1 if FAILS else 0)
