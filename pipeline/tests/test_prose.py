"""Sentences across page breaks, text too small to read, and paragraphs placed in tables. The HTML is cut from real
filings (TELA Bio S-1 0001104659-20-076043, Roivant 8-K 0001193125-21-284927, TELA DEF 14A 0001104659-20-050364).

    python pipeline/tests/test_prose.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evidence"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prose_reader as P  # noqa: E402
import evidence_tables as E  # noqa: E402

FAILS = []


def check(name, got, want):
    ok = got == want
    print("OK " if ok else "BAD", name)
    if not ok:
        print("    got ", repr(got))
        print("    want", repr(want))
        FAILS.append(name)


def mention(html, word="Lungpacer", nth=0):
    d = P.Doc(html)
    m = list(re.finditer(word, html))[nth]
    return d.mention(m.start(), m.end())


# 1. A sentence cut by a page break: page number, rule and "TABLE OF CONTENTS" link between its halves (TELA S-1)
PAGE = ('<div><div style="text-align:justify">Mr.&#160;Evans has served as the President of Lungpacer Medical Inc. since January&#160;2014. Prior to </div></div>'
        '<div><div>&#160;<br></div></div><div><div style="text-align:center">25<br></div></div><hr style="page-break-after:always">'
        '</div><div><div><a href="#TOC">TABLE OF CONTENTS</a></div><div><div>&#160;<br></div></div>'
        '<div><div style="text-align:justify">joining Lungpacer, Mr.&#160;Evans served as the Chief Operating Officer of Kensey Nash Corporation.</div></div>')
check("page break: second half", mention(PAGE, nth=1)["sentence"],
      "Prior to joining Lungpacer, Mr. Evans served as the Chief Operating Officer of Kensey Nash Corporation.")
check("page break: sentence before the break", mention(PAGE, nth=0)["sentence"],
      "Mr. Evans has served as the President of Lungpacer Medical Inc. since January 2014.")

# 2. No join when the text before the break ends a sentence, or the next page starts a new one with a heading
ENDED = ('<p>Lungpacer paid the note in full.</p><p>12</p><hr><p>Table of Contents</p><p>Risk Factors</p>'
         '<p>We may need more capital to grow Lungpacer sales in the coming years.</p>')
check("page break: ended sentence stays", mention(ENDED, nth=0)["sentence"], "Lungpacer paid the note in full.")
check("page break: heading not joined", mention(ENDED, nth=1)["sentence"], "We may need more capital to grow Lungpacer sales in the coming years.")

# 3. A running header repeated after every break is furniture too
HEAD = "".join(f'<p>Body text {i}.</p><hr><p>TELA BIO, INC. PROSPECTUS</p>' for i in range(3)) + \
       ('<p>He was the chief operating officer of</p><p>7</p><hr><p>TELA BIO, INC. PROSPECTUS</p>'
        '<p>the medical device company Lungpacer from 2014 to 2020.</p>')
check("page break: running header skipped", mention(HEAD)["sentence"],
      "He was the chief operating officer of the medical device company Lungpacer from 2014 to 2020.")

# 4. The text layer printed under a slide picture in a 1pt white font (Roivant 8-K, Slide 47)
SLIDE = ('<p style="margin-top:0pt"><img src="g190161ex99_1s47g1.jpg" alt="Slide 47" title="Slide 47"></p>'
         '<p style="margin-top:0pt;margin-bottom:0pt;font-size:1pt;line-height:0pt;color:#FFFFFF;">Tracy Meffen VP Quality &amp; Regulatory '
         'Former Head Quality, Arbutus Biopharma Over 25 years experience in QA and RA management roles at various organizations including '
         'Lungpacer, INEX, Lilly and Genzyme Ed Yaworski VP Pharmaceutical Development</p>')
x = mention(SLIDE)
check("tiny text: kind", x["kind"], "hidden text")
check("tiny text: names the picture", "under the picture “Slide 47”" in x["block"], True)
check("tiny text: words around the name", "including Lungpacer, INEX" in x["sentence"], True)

# 5. A 1pt parent whose child sets a readable size again is visible text
NESTED = ('<div style="font-size:1pt"><p style="font-size:10pt">The Company invested in Lungpacer Medical Inc. in 2019.</p></div>')
check("tiny parent, readable child: a sentence", mention(NESTED)["kind"], "sentence")
check("tiny: 10pt is readable", P.tiny_font("font-size:10pt"), False)
check("tiny: 0.5pt", P.tiny_font("font-size: 0.5pt"), True)
check("tiny: 0em", P.tiny_font("font-size:0em"), True)
check("tiny: 1em is readable", P.tiny_font("font-size:1em"), False)

# 6. visibility:hidden is hidden
check("visibility hidden", mention('<p style="visibility: hidden">Lungpacer note</p>')["kind"], "hidden")

# 7. A director's bio placed in a one-cell layout table is prose; a holdings row is data (TELA DEF 14A)
BIO = [(0, 0, "Doug Evans has been a member of our Board since April 2020. Mr. Evans has served as the President and Chief Executive "
              "Officer of Lungpacer Medical Inc., a medical device company, since January 2014. Prior to joining Lungpacer, Mr. Evans served "
              "as the Chief Operating Officer and a member of the board of directors of Kensey Nash Corporation.")]
check("table: bio cell is prose", E.prose_cell(BIO, "lungpacer"), True)
SUMMARY = [(0, 0, "Doug Evans"), (1, 1, "56"), (2, 2, "—"), (3, 3, "Chief Executive Officer of Lungpacer Medical Inc."), (4, 4, "Yes")]
check("table: board summary row is data", E.prose_cell(SUMMARY, "lungpacer"), False)
HOLD = [(0, 0, "Lungpacer Medical Inc., Series B Preferred Stock (acquired 03/01/20, cost $1,000,000) (a)(b)(c) Restricted security; "
              "Level 3; the fund values the position at the price of the last round in which the company sold shares to investors."),
        (1, 1, "1,250,000"), (2, 2, "1,000,000"), (3, 3, "1,180,000")]
check("table: long holdings description with amounts is data", E.prose_cell(HOLD, "lungpacer"), False)
NOTE = [(0, 0, "(a) Lungpacer Medical Inc. is a private company. The fund acquired its shares in a private placement and may not sell "
              "them without registration under the Securities Act or an exemption from it; the shares are valued by the adviser in good faith.")]
check("table: footnote paragraph in a table is prose", E.prose_cell(NOTE, "lungpacer"), True)

print("failures:", len(FAILS))
assert not FAILS
