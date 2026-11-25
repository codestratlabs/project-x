#!/usr/bin/env python3
"""Lines of a filing made of absolutely placed text boxes (HTML converted from PDF: every word or cell is a
<div style="position:absolute; top:..; left:..">). Boxes on the same line (same top) are one printed line, ordered by
left; white text (invisible filler) is dropped. A table row is such a line, and its column headers are the lines above
it that name columns (Shares, Value, Cost ...).
"""
from __future__ import annotations

import bisect
import html
import re

BOX = re.compile(r"<div\b([^>]*\bstyle\s*=\s*\"[^\"]*position\s*:\s*absolute[^\"]*\"[^>]*)>(.*?)</div>", re.I | re.S)
PX = lambda st, k: (lambda m: float(m[1]) if m else None)(re.search(rf"(?<![-\w]){k}\s*:\s*(-?[\d.]+)px", st))   # noqa: E731
WHITE = re.compile(r"(?<![-\w])color\s*:\s*(?:#fff(?:fff)?\b|white\b|rgb\(\s*255\s*,\s*255\s*,\s*255\s*\))", re.I)


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", s)).replace("\xa0", " ").replace("​", " ").replace("‌", "")).strip()


class Layout:
    def __init__(self, t: str):
        boxes = []
        for m in BOX.finditer(t):
            st = re.search(r"style\s*=\s*\"([^\"]*)\"", m[1], re.I)[1]
            top, left = PX(st, "top"), PX(st, "left")
            if top is None or left is None or WHITE.search(st):
                continue
            txt = clean(m[2])
            if not txt:
                continue
            fs = PX(st, "font-size") or 10.0
            boxes.append((m.start(), m.end(), top, left, fs, txt))      # width is not printed: about half the font size per character
        self.lines = []                                  # [(start, end, top, [(left, width, text)])]
        for b in boxes:                                  # boxes come in reading order; a new top starts a new line
            if self.lines and abs(b[2] - self.lines[-1][2]) <= 0.3 * b[4] and b[0] - self.lines[-1][1] < 5000:
                ln = self.lines[-1]
                ln[3].append((b[3], 0.5 * b[4] * len(b[5]), b[5]))
                self.lines[-1] = (ln[0], b[1], ln[2], ln[3])
            else:
                self.lines.append((b[0], b[1], b[2], [(b[3], 0.5 * b[4] * len(b[5]), b[5])]))
        for ln in self.lines:
            ln[3].sort()
        self.starts = [ln[0] for ln in self.lines]
        self.ok = len(boxes) > 200

    def line_at(self, pos: int):
        i = bisect.bisect_right(self.starts, pos) - 1
        if i >= 0 and self.lines[i][0] <= pos < self.lines[i][1] + 1:
            return i
        return None

    def text(self, i: int) -> str:
        return " ".join(c[2] for c in self.lines[i][3])


NUM = re.compile(r"^\$?\s*\(?\s*\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*\)?$")
KEYWORD = re.compile(r"shares|principal|par\b|value|cost|units|amount|face|quantity|commitment|number of", re.I)


VOCAB = re.compile(r"^(?:shares?|par|principal|amount|value|fair|market|cost|amortized|units?|number|of|\$|\(\$?\s*0{3}[’'`]?s?\)|\(\$\)|face|quantity|"
                   r"commitments?|unfunded|country|acquisition|acquired|date|%|net|assets|percent(?:age)?|rate|maturity|interest|/|shares/par|par/shares|"
                   r"principal/shares|in|\$000s?|000s?|\(000s?\)|or|and|contracts?|notional)$", re.I)
NUMERIC_ROLES = ("qty", "cost", "value", "commitment", "funded")


def number(c: str):
    """A cell that is only a number (with $, brackets, commas): its value with sign; None otherwise (dates, %, words,
    footnote marks like (8))."""
    s = c.strip().rstrip("*†‡")
    m = NUM.match(s)
    if not m or re.search(r"\d/\d|%", s) or s.count("(") != s.count(")"):
        return None
    if "(" in s and len(m[1]) <= 2:
        return None                                  # (8): a footnote mark
    v = float(m[1].replace(",", ""))
    return -v if "(" in s else v


def is_header_line(cells: list) -> bool:
    """Only column-label words, at least one naming a numeric column, and no numbers."""
    words = [w for c in cells for w in re.split(r"\s+", c[2]) if w]
    return bool(words) and all(VOCAB.match(w) for w in words) and any(KEYWORD.search(c[2]) for c in cells)


def header_columns(L: Layout, i: int, col_of, want: int) -> list:
    """Column labels of the nearest header above line i that has exactly `want` numeric columns: [(x1, label, role)].
    Words of one label can sit on two or three lines ("Fair" over "Value"); words whose boxes overlap in x are one column."""
    j = i - 1
    while j >= 0:
        if not is_header_line(L.lines[j][3]):
            j -= 1
            continue
        block = [j]                                   # this header line and the header lines right above it
        k = j - 1
        while k >= 0 and is_header_line(L.lines[k][3]) and j - k < 4:
            block.append(k)
            k -= 1
        cols = []
        for b in sorted(block):
            for left, w, txt in L.lines[b][3]:
                for c in cols:
                    if left < c[1] + 0.2 * w and left + w > c[0] - 0.2 * w:
                        c[0], c[1] = min(c[0], left), max(c[1], left + w)
                        c[2].append(txt)
                        break
                else:
                    cols.append([left, left + w, [txt]])
        out = [(x1, " ".join(ws), col_of(" ".join(ws))) for x0, x1, ws in sorted(cols)]
        if sum(1 for o in out if o[2] in NUMERIC_ROLES) == want:
            return out
        j = k
    return []


def filing_header(L: Layout, col_of, want: int) -> list:
    """The column order the filing's own schedule headers print, when the header is not on the row's page: the numeric
    column labels of every header line in the filing (label words only, other words on the line ignored), in x order;
    the most common sequence with `want` columns. [] when the filing prints none."""
    if not hasattr(L, "_seqs"):
        seqs = []
        for i, ln in enumerate(L.lines):
            cells = ln[3]
            if any(number(c[2]) is not None for c in cells):
                continue
            labs = [c for c in cells if all(VOCAB.match(w) for w in c[2].split()) and KEYWORD.search(c[2])]
            if not labs:
                continue
            roles = [col_of(c[2]) for c in sorted(labs)]
            seq = tuple(r for r in roles if r in NUMERIC_ROLES)
            if seq:
                seqs.append((seq, " | ".join(c[2] for c in sorted(labs))))
        L._seqs = seqs
    from collections import Counter
    cnt = Counter(s for s, _ in L._seqs if len(s) == want and len(set(s)) == want)
    if not cnt:
        return []
    seq = cnt.most_common(1)[0][0]
    label = next(l for s, l in L._seqs if s == seq)
    return [(None, label, r) for r in seq]


def record(L: Layout, i: int, name_left: float) -> tuple:
    """(cells of the holding, line index where its numbers are): the name's line, and the next lines while the holding's
    text runs on (no number yet, the next line starts at or right of the name)."""
    cells = list(L.lines[i][3])
    j = i
    while not any(number(c[2]) is not None and c[0] > name_left for c in cells) and j + 1 < len(L.lines) and j - i < 3:
        nxt = L.lines[j + 1][3]
        if nxt[0][0] < name_left - 50 and not any(number(c[2]) is not None for c in nxt):
            break
        j += 1
        cells += nxt
    return cells, j


def assign(L: Layout, i: int, cells: list, col_of) -> tuple:
    """({role: cell text}, {role: header words}, why) for the record's numbers. Each number takes the column label printed
    above it: the nearest label words above whose right edge lines up with the number's right edge (numbers and their
    labels are right-aligned). A number with no label above it, or two numbers under one label, is not given a column:
    nothing is assigned by order alone."""
    nums, depth = [], 0
    for c in cells:                                  # a number inside the name's brackets is part of the name: "(Acquired
        if number(c[2]) is not None:                 # 10/22/19, cost $3,030,0 | 10 )" split one printed cost over two boxes
            if depth == 0:
                nums.append(c)
        else:
            depth = max(0, depth + c[2].count("(") - c[2].count(")"))
    if not nums:
        return {}, {}, "no number in the row"
    if not hasattr(L, "_width"):
        L._width = max((c[0] + c[1] for ln in L.lines for c in ln[3]), default=1.0)
    tol = 0.035 * L._width
    got, labels, why, left, at = {}, {}, [], [], []
    for c in nums:
        right = c[0] + c[1]
        words = []
        for j in range(i - 1, max(-1, i - 400), -1):
            cs = L.lines[j][3]
            if any(number(x[2]) is not None for x in cs):
                if words:
                    break                                # the label block ends at a line of numbers
                continue
            hit = [x for x in cs if abs(x[0] + x[1] - right) <= tol and all(VOCAB.match(w) for w in x[2].split())]
            if hit:
                words.insert(0, " ".join(x[2] for x in hit))      # "$" and "Value" in two boxes are one label
                at.append(j)
            elif words:
                break
        label = " ".join(words)
        role = col_of(label) if label else None
        if role not in NUMERIC_ROLES:
            left.append(c)
            continue
        if role in got:
            why.append(f"{c[2]}: two numbers under '{label}'")
            continue
        got[role], labels[role] = c[2], label
    if len(left) == 1 and got and at:
        # one number left without a label above it (the converter lost that label's position): the header lines next to
        # the labels found must name exactly one more numeric column, and the number takes it
        names = set()
        for j in range(max(0, min(at) - 2), min(len(L.lines), max(at) + 2)):
            for x in L.lines[j][3]:
                if all(VOCAB.match(w) for w in x[2].split()) and col_of(x[2]) in NUMERIC_ROLES and col_of(x[2]) not in got:
                    names.add((col_of(x[2]), x[2]))
        if len({r for r, _ in names}) == 1:
            r, lab = sorted(names)[0]
            got[r], labels[r] = left[0][2], lab + " (label printed on the header line, position not printed)"
            left = []
    why += [f"{c[2]}: no column label above it" for c in left]
    return got, labels, "; ".join(why)
