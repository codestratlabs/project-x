#!/usr/bin/env python3
"""Page numbers as printed in a filing. Shared by locate.py (string-match evidence) and indirect_locate.py (pronouns)."""
from __future__ import annotations

import bisect
import html
import re

BREAK = re.compile(r"page-break-(?:before|after)\s*:\s*always|<hr\b[^>]*>", re.I)


def txt(x: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", x)).replace("\xa0", " ")).strip()


FOOT = re.compile(r"(?:^|\s)(?:page\s+)?[-–—]?\s*((?:[A-Z]{1,2}\s?-\s?)?\d{1,4})\s*[-–—]?\s*$", re.I)


def page_at(t: str, breaks: list, pos: int) -> str:
    """The page number printed at the foot of the page the row is on ("- 13 -", "F-12"); if none is printed, the page
    counted from the filing's page breaks, marked as counted."""
    if len(breaks) < 3:
        return "no page numbers in this filing"
    i = bisect.bisect_right(breaks, pos)
    end = breaks[i] if i < len(breaks) else len(t)
    end = t.rfind("<", max(pos, end - 4000), end) if t.rfind("<", max(pos, end - 4000), end) > pos else end   # the break sits inside a tag
    tail = txt(t[max(pos, end - 4000):end])
    m = re.search(r"(?:^|\s)((?:[A-Z]{1,2}-)?\d{1,4})\s+(?:19|20)\d\d\s+[A-Z][^.]{0,70}(?:Report|Shareholders|Stockholders)[^.]{0,30}$", tail)
    if m:
        return m[1]                                    # "92 2025 BlackRock Semi-Annual Report to Shareholders"
    tail = re.sub(r"(?:Table of Contents|See (?:accompanying )?Notes to (?:Consolidated )?Financial Statements\.?|The accompanying notes[^.]*\.)\s*$", "", tail, flags=re.I).strip()
    tail = re.sub(r"\s*See Accompanying Notes\.?$", "", tail, flags=re.I).strip()
    m = FOOT.search(tail[-40:])
    if m and not re.fullmatch(r"(19|20)\d\d", m[1]):
        return re.sub(r"\s", "", m[1])
    m = re.search(r"[A-Za-z]-(\d{1,4})$", tail)                  # "BHFTI-5", "... Fund-6"
    if m:
        return m[1]
    m = re.search(r"(?:^|\s)(\d{1,4})\s+([A-Z][^\d]{3,80})$", tail)   # "4 NYLI VP Winslow Large Cap Growth Portfolio"
    if m and not re.fullmatch(r"(19|20)\d\d", m[1]):
        return m[1]
    return f"{i + 1} (counted)"


def load(path) -> tuple[str, list]:
    """The filing text (table tags lower-cased) and the positions of its page breaks."""
    t = re.sub(r"</?(?:table|tr|td|th)\b", lambda x: x[0].lower(), path.read_text(encoding="utf-8", errors="ignore"), flags=re.I)
    return t, [m.start() for m in BREAK.finditer(t)]
