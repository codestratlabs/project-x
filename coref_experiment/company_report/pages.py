#!/usr/bin/env python3
"""Page numbers as printed in a filing. Shared by locate.py (string-match evidence) and indirect_locate.py (pronouns)."""
from __future__ import annotations

import bisect
import functools
import html
import re

# page-break marks: the old CSS (page-break-before/after: always), the new CSS (break-before/after: page) and <hr>
BREAK = re.compile(r"page-break-(?:before|after)\s*:\s*always|break-(?:before|after)\s*:\s*page|<hr\b[^>]*>", re.I)


def txt(x: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", x)).replace("\xa0", " ")).strip()


FOOT = re.compile(r"(?:^|\s)(?:page\s+)?[-–—]?\s*((?:[A-Z]{1,2}\s?-\s?)?\d{1,4})\s*[-–—]?\s*$", re.I)
ROMAN = r"(?=[ivx])x{0,3}(?:ix|iv|v?i{0,3})"                     # front-matter page numbers, lower case: i, ii, iv, x ...
ROMAN_END = re.compile(rf"(?:^|[.;:!?)|]\s*)({ROMAN})$")          # "... Proxy Statement | ii", "... the end. iv"
NUM_FIRST = re.compile(rf"(?:^|\s)(\d{{1,4}}|{ROMAN})\s*\|\s*[A-Z][^|]{{3,100}}$")   # "8 | Notice of Annual Meeting ..."
# a running footer written at the start of the page's HTML (PDF-converted reports), parts split by "|", then the number:
# "Annual Report 2025 | Corporate governance 116", "Advisory vote | Corporate governance and compensation | Compensation 234"
HEAD = re.compile(r"^[A-Z][^|\d]{1,60}?(?:\s(?:19|20)\d\d)?(?:\s*\|\s*[^|\d]{2,80}?){1,4}\s+(\d{1,4})\b")


@functools.lru_cache(maxsize=2)
def _hidden(t: str) -> tuple:
    """Where the hidden XBRL block(s) sit, found once per filing text."""
    return tuple((m.start(), m.end()) for m in re.finditer(r"<ix:header\b.*?</ix:header>", t, re.S | re.I))


def _in_tag(t: str, pos: int) -> bool:
    """True when pos sits inside a tag (a page-break style attribute)."""
    return t.rfind("<", 0, pos) > t.rfind(">", 0, pos)


def breaks_of(t: str) -> list:
    """Where the filing's pages end, from the page-break marks, with two corrections:
    - a page-break-after on a short element ('<p style="page-break-after: always">252</p>') ends the page after that
      element: the number printed inside it is the number of the page before the break, not of the next page;
    - marks with nothing printed between them (a rule under the footer, then the next page's box) are one page end."""
    out = []
    for m in BREAK.finditer(t):
        pos = m.start()
        if "after" in m[0].lower() and _in_tag(t, pos):
            s, gt = t.rfind("<", 0, pos), t.find(">", pos)
            name = re.match(r"<([A-Za-z][\w:-]*)", t[s:s + 40])
            close = re.compile(rf"</{re.escape(name[1])}\s*>", re.I).search(t, gt, gt + 2000) if name and gt > 0 else None
            inner = t[gt + 1:close.start()] if close else ""
            if close and len(txt(inner)) <= 20 and not re.search(rf"<{re.escape(name[1])}\b", inner, re.I):
                pos = close.end()
        if out:
            a = t.find(">", out[-1]) + 1 if _in_tag(t, out[-1]) or t.startswith("<", out[-1]) else out[-1]
            b = t.rfind("<", 0, pos) if _in_tag(t, pos) else pos
            if not txt(t[a:b]):
                continue
        out.append(pos)
    return out


def page_at(t: str, breaks: list, pos: int) -> str:
    """The page number printed on the page the row is on: at its foot ("- 13 -", "F-12", "Proxy Statement | 7",
    "8 | Notice of ...", "ii") or in a running footer written at the start of the page; if none is printed, the page
    counted from the filing's page breaks, marked as counted. Data in the hidden XBRL block is on no page."""
    if any(a <= pos < b for a, b in _hidden(t)):
        return "hidden XBRL data, not on a page"
    if len(breaks) < 3:
        return "no page numbers in this filing"
    i = bisect.bisect_right(breaks, pos)
    end = breaks[i] if i < len(breaks) else len(t)
    end = t.rfind("<", max(pos, end - 4000), end) if t.rfind("<", max(pos, end - 4000), end) > pos else end   # the break sits inside a tag
    tail = txt(t[max(pos, end - 4000):end])
    m = re.search(r"(?:^|\s)((?:[A-Z]{1,2}-)?\d{1,4})\s+(?:19|20)\d\d\s+[A-Z][^.]{0,70}(?:Report|Shareholders|Stockholders)[^.]{0,30}$", tail)
    if m:
        return m[1]                                    # "92 2025 BlackRock Semi-Annual Report to Shareholders"
    start = breaks[i - 1] if i else 0
    start = t.find(">", start) + 1 if i and (_in_tag(t, start) or t.startswith("<", start)) else start
    m = HEAD.search(txt(t[start:min(start + 3000, end)])[:300])
    if m:
        return m[1]                                    # "Annual Report 2025 | Corporate governance 116" at the page's start
    tail = re.sub(r"(?:Table of Contents|See (?:accompanying )?Notes to (?:Consolidated )?Financial Statements\.?|The accompanying notes[^.]*\.)\s*$", "", tail, flags=re.I).strip()
    tail = re.sub(r"\s*See Accompanying Notes\.?$", "", tail, flags=re.I).strip()
    m = FOOT.search(tail[-40:])
    if m and not re.fullmatch(r"(19|20)\d\d", m[1]):
        return re.sub(r"\s", "", m[1])
    m = ROMAN_END.search(tail[-40:])
    if m:
        return m[1]
    m = re.search(r"[A-Za-z]-(\d{1,4})$", tail)                  # "BHFTI-5", "... Fund-6"
    if m:
        return m[1]
    m = NUM_FIRST.search(tail)
    if m and not re.fullmatch(r"(19|20)\d\d", m[1]):
        return m[1]                                    # "8 | Notice of Annual Meeting of Stockholders and 2022 Proxy Statement"
    m = re.search(r"(?:^|\s)(\d{1,4})\s+([A-Z][^\d]{3,80})$", tail)   # "4 NYLI VP Winslow Large Cap Growth Portfolio"
    if m and not re.fullmatch(r"(19|20)\d\d", m[1]):
        return m[1]
    return f"{i + 1} (counted)"


def load(path) -> tuple[str, list]:
    """The filing text (table tags lower-cased) and the positions where its pages end."""
    t = re.sub(r"</?(?:table|tr|td|th)\b", lambda x: x[0].lower(), path.read_text(encoding="utf-8", errors="ignore"), flags=re.I)
    return t, breaks_of(t)
