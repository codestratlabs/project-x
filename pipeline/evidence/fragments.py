"""Link text that lands on its own row.

A link to a filing carries the text to highlight (#:~:text=...). The browser opens the filing at the FIRST place that text
is printed, case ignored, inside one paragraph or table cell. A short text printed in several places (a number such as
"14", a bullet, a sentence a 10-Q prints again for last year) opens the filing at another row. Here the text a row's link
searches for is checked against the whole filing as a browser reads it:
  - it is kept when the row's own copy is already the first one in the filing (the link lands there);
  - otherwise it is lengthened a word at a time, inside the row's own line, until the filing prints it only there; a table
    row may use another of its own cells instead;
  - when nothing in the row is printed only there (the same heading on twenty pages), the link opens the filing without a
    search: it never lands at another row.
"""
from __future__ import annotations

import bisect
import html
import re

import prose_reader as PR   # the filing's blocks (paragraphs, cells) and hidden parts, as the rest of the pipeline reads them

TOKEN = re.compile(r"<br\s*/?>|<[^>]*>|&[#\w]+;|\s+|[^<&\s]+|[<&]", re.I)
BR = re.compile(r"<br\s*/?>", re.I)
SEP = "\x00"                                                  # between blocks: no search text crosses it
MAX_LEN = 300                                                 # longest search text written into a link


def low(s: str) -> str:
    """Lower case, one character for one character (so positions stay the same)."""
    return "".join(c.lower() if len(c.lower()) == 1 else c for c in s)


def browser_text(x: str) -> str:
    """The text findings_excel.link() writes into a link (it must stay the same as that function's cleaning)."""
    return re.sub(r"\s+([,.;:)%])", r"\1", re.sub(r"\(\s+", "(", " ".join(x.split())))


def shown(t: str, a: int, b: int, hidden=None) -> tuple:
    """The block t[a:b] as a browser shows it: tags dropped (an inline tag adds no space), a <br> kept as a line break
    ('\\n'), entities read, other white space one space, text inside a hidden part left out (hidden(pos) -> True).
    Returns (text, raw position of each character)."""
    out, pos, space = [], [], True
    for m in TOKEN.finditer(t, a, b):
        tok, r0 = m[0], m.start()
        if hidden is not None and tok[0] != "<" and hidden(r0):
            continue
        if tok[0] == "<" and len(tok) > 1:
            if BR.fullmatch(tok):
                if out and out[-1] == " ":
                    out[-1] = "\n"
                elif out and out[-1] != "\n":
                    out.append("\n")
                    pos.append(r0)
                space = True
            continue
        if tok[0] == "&" and len(tok) > 1:
            tok, step = html.unescape(tok), 0
        else:
            step = 1
        for j, ch in enumerate(tok):
            if ch in "​‌‍﻿":
                continue
            if ch.isspace() or ch == "\xa0":
                if space:
                    continue
                out.append(" ")
                space = True
            else:
                out.append(ch)
                space = False
            pos.append(r0 + j * step)
    while out and out[-1] in " \n":
        out.pop()
        pos.pop()
    return "".join(out), pos


class Page:
    """A filing as a browser reads it for a link: its blocks in order, hidden parts left out, all in one lower-case string
    (blocks apart) to count where a text is printed."""

    def __init__(self, t: str):
        self.t, self.doc = t, PR.Doc(t)
        spans, texts = [], []
        n = len(self.doc.bstart)
        for k in range(n + 1):
            a = self.doc.bend[k - 1] if k > 0 else 0
            b = self.doc.bstart[k] if k < n else len(t)
            if b <= a:
                continue
            s = shown(t, a, b, self.hidden)[0]
            if s:
                spans.append((a, b))
                texts.append(s)
        self.spans, self.texts = spans, texts
        self.starts = [a for a, _ in spans]
        self.offs, o = [], 0
        for s in texts:
            self.offs.append(o)
            o += len(s) + 1
        self.big = low(SEP.join(s.replace("\n", " ") for s in texts))

    def hidden(self, pos: int) -> bool:
        i = bisect.bisect_right(self.doc.hstart, pos) - 1
        return i >= 0 and self.doc.hid[i][0] <= pos < self.doc.hid[i][1]

    def block_at(self, pos: int):
        i = bisect.bisect_right(self.starts, pos) - 1
        return i if i >= 0 and self.spans[i][0] <= pos < self.spans[i][1] else None

    def count(self, q: str) -> int:
        return self.big.count(low(q)) if q else 0

    def first(self, q: str) -> int:
        return self.big.find(low(q)) if q else -1


def _own_copy(page: Page, own: tuple, st: str):
    """(block index, start of the row's own copy of st in that block's shown text), or None."""
    q = low(st)
    if own[0] == "pos":
        i = page.block_at(own[1])
        if i is None:
            return None
        s, posmap = shown(page.t, *page.spans[i], page.hidden)
        at = bisect.bisect_left(posmap, own[1])
        hits = [m.start() for m in re.finditer(re.escape(q), low(s))]
        if not hits:
            return None
        inside = [h for h in hits if h <= at < h + len(q)]
        return i, (inside[0] if inside else min(hits, key=lambda h: abs(h - at)))
    _, a, b = own                                             # a table row: the first of its cells that holds st
    for i in range(bisect.bisect_left(page.starts, a), len(page.spans)):
        if page.spans[i][0] >= b:
            break
        h = low(page.texts[i]).find(q)
        if h >= 0:
            return i, h
    return None


MAX_GAP = 2000                                                # most text between a range's start and the row (keeps the row on screen)


def _range_start(page: Page, i: int, h: int, q_st: str) -> str:
    """For a row whose own text is printed elsewhere too: the nearest text above it, within MAX_GAP characters, that the
    filing prints only once, with no other copy of the row's text between it and the row. A link "start,end" then opens
    at the row: the browser finds the start, then the first copy of the row's text after it. The start stays inside one
    line (a browser does not match across a line break). '' when there is none."""
    g, q, gap = page.offs[i] + h, low(q_st), 0
    for j in range(i - 1, -1, -1):
        lines = page.texts[j].split("\n")
        gap += len(page.texts[j])
        if gap > MAX_GAP:
            return ""
        for line in reversed(lines):                          # the block's last line first, nearest the row
            words = list(re.finditer(r"\S+", line))
            for k in range(1, len(words) + 1):                # the line's last words, then more of it, the shortest first
                c = line[words[-k].start():]
                if len(c) > MAX_LEN:
                    break
                if len(c) < 4 or not re.search(r"[A-Za-z0-9]", c) or browser_text(c) != c or page.count(c) != 1:
                    continue
                gc = page.first(c)
                return c if page.big.find(q, gc + len(c)) == g else ""   # another copy in between: no start above can skip it
    return ""


def own_link(page: Page, own: tuple, st: str, prefix: str = "") -> dict:
    """How a row's link should search the filing. own: ("pos", raw position of the name) for a sentence or a tag, or
    ("row", raw start, raw end) of the table row. Returns {"mode", "start", "end", "check"}:
      keep   the link as it is: the row's own copy of its text (with its prefix) is the first in the filing;
      text   search for "start", a longer text printed only on this row (or another cell of the row);
      range  search "start,end": start printed only once just above the row, end the row's own text;
      plain  open the filing without a search: nothing near the row is printed only there."""
    out = lambda mode, check, start="", end="": {"mode": mode, "start": start, "end": end, "check": check}   # noqa: E731
    if not st:
        return out("keep", "")
    q_st, q_pre = browser_text(st), browser_text(prefix) if prefix else ""
    hit = _own_copy(page, own, q_st)
    if hit is None:
        return out("keep", "kept: its own copy was not found in the browser text")
    i, h = hit
    g = page.offs[i] + h                                      # where the own copy starts in page.big
    qs = [low(q_pre) + " " + low(q_st), low(q_pre) + low(q_st)] if q_pre else [low(q_st)]
    found = [(page.first(q), q) for q in qs if page.first(q) >= 0]
    if found:
        first, q = min(found)
        if first + len(q) - len(q_st) == g:                   # the browser's first match is the row's own copy
            n = page.count(q)
            return out("keep", "lands on its row: " + ("printed only here" if n == 1 else f"first of {n} copies"))
    s = page.texts[i]                                         # lengthen inside the row's own line, a word at a time
    ls = s.rfind("\n", 0, h) + 1
    le = s.find("\n", h + len(q_st))
    le = len(s) if le < 0 else le
    words = [(ls + m.start(), ls + m.end()) for m in re.finditer(r"\S+", s[ls:le])]
    i0 = next((k for k, (x, y) in enumerate(words) if y > h), None)
    i1 = next((k for k, (x, y) in enumerate(words) if y >= h + len(q_st)), None)
    if i0 is not None and i1 is not None:
        for k in range(0, len(words)):
            for left in range(0, k + 1):
                right = k - left
                if i0 - left < 0 or i1 + right >= len(words):
                    continue
                c = s[words[i0 - left][0]:words[i1 + right][1]]
                if len(c) > MAX_LEN or browser_text(c) != c:
                    continue
                if page.count(c) == 1:
                    return out("text", f"made unique: {k} word(s) added" if k else "made unique: whole words", c)
    if own[0] == "row":                                       # a table row: another of its own cells, printed only here
        _, a, b = own
        for j in range(bisect.bisect_left(page.starts, a), len(page.spans)):
            if page.spans[j][0] >= b:
                break
            c = page.texts[j]
            if j != i and "\n" not in c and len(c) >= 2 and re.search(r"[A-Za-z0-9]", c) and len(c) <= MAX_LEN and browser_text(c) == c and page.count(c) == 1:
                return out("text", "made unique: another cell of the row", c)
    start = _range_start(page, i, h, q_st)
    if start:
        return out("range", "range: a text printed once just above, then the row's own text", start, q_st)
    return out("plain", "plain link: nothing near the row is printed only there")
