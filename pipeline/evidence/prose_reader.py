#!/usr/bin/env python3
"""The sentence that names the company, read from the filing's HTML by its structure (no fixed-width slices).

A mention the table readers did not take is one of:
  hidden        not on a page: inside <ix:header>, <head>, <script>, <style>, an HTML comment, a display:none or
                visibility:hidden element, or inside a tag (an attribute value)
  hidden text   on the page but too small to read: an element with a font size of 1pt / 1px or less (the text layer
                that converted slide decks print under each slide picture); listed with the words around the name
  sentence      running text: the sentence around the name, cut at sentence ends inside its own block
                (<p>, <div>, <li>, table cell, heading)
  table cell    a cell of a table that is not a sentence (a name, a label, a number)
  heading or label   a block outside tables that is not a sentence

Only whole blocks are read, so no tag or style text can leak into the sentence. A sentence that runs across a page
break is joined again, and the page furniture between its halves (page number, "Table of Contents" link, a running
header or footer printed on every page) is left out.
"""
from __future__ import annotations

import bisect
import html
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sentences as S  # noqa: E402  sentence ends that know abbreviations (Mr., Inc., initials)

BLOCK = re.compile(r"</?(?:p|div|li|ul|ol|td|th|tr|table|h[1-6]|blockquote|center|section|article|body|html|title|dt|dd|pre|hr)\b[^>]*>|<br\s*/?>\s*(?:&nbsp;|\s)*<br\s*/?>", re.I)
HIDDEN = re.compile(r"<ix:header\b.*?</ix:header>|<head\b.*?</head>|<script\b.*?</script>|<style\b.*?</style>|<!--.*?-->|<xbrli?:[a-z]+\b[^>]*>.*?</xbrli?:[a-z]+>", re.I | re.S)
VOID = {"br", "img", "hr", "input", "meta", "link", "col", "area", "base", "wbr", "source", "embed"}
NONE = re.compile(r"<([a-z][a-z0-9:]*)\b[^>]*\bstyle\s*=\s*[\"'][^\"']*(?:display\s*:\s*none|visibility\s*:\s*hidden)[^\"']*[\"'][^>]*>", re.I)
STYLED = re.compile(r"<([a-z][a-z0-9:]*)\b[^>]*\bstyle\s*=\s*[\"']([^\"']*font-size\s*:[^\"']*)[\"'][^>]*>", re.I)
FONT_SIZE = re.compile(r"font-size\s*:\s*(\d*\.?\d+)\s*(pt|px|em|rem|%)?", re.I)
PAGE_BREAK = re.compile(r"<hr\b[^>]*>|<[a-z][^>]*(?:page-break-(?:before|after)|break-(?:before|after))\s*:\s*(?:always|page)[^>]*>", re.I)
FURNITURE = re.compile(r"(?:page\s+)?(?:[A-Z]{1,2}\s?-\s?)?\d{1,4}(?:\s+of\s+\d{1,4})?|[ivxlcdm]{1,7}|table\s+of\s+contents|index\s+to\s+(?:consolidated\s+)?financial\s+statements"
                       r"|back\s+to\s+(?:top|contents|table\s+of\s+contents)|return\s+to\s+(?:top|table\s+of\s+contents)|\(?continued\)?|", re.I)
TERMINAL = re.compile(r"[.!?:][\"”’')\]]*$")
CONNECTOR = re.compile(r"(?:[,;(\-–—]|\b(?:the|a|an|of|and|or|to|in|for|by|with|on|at|from|as|its|our|their|his|her|that|which|who|is|was|were|be|been|has|have|had|Mr\.|Mrs\.|Ms\.|Dr\.))$", re.I)
WORD = re.compile(r"[A-Za-z][A-Za-z’'-]*")


MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
QUARTER = re.compile(r"\bQ([1-4])\s+((?:19|20)\d{2})\b")
ENDED = re.compile(r"\b(?:(?:three|six|nine|twelve)\s+months|(?:fiscal\s+)?years?|quarters?)\s+ended\s+(" + MONTH + r")\s+(\d{1,2}),\s*((?:19|20)\d{2})"
                   r"(?:\s*(?:,|and)\s*((?:19|20)\d{2}))?", re.I)


def to_date(s: str):
    import datetime as dt
    for f in ("%B %d, %Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(" ".join((s or "").split()), f).date()
        except ValueError:
            pass
    return None


def named_periods(s: str, per) -> set:
    """The period end dates a text names. "Q1 2024" is dated only when its quarter ends in the filing's period month
    (Q1 = March, ... Q4 = December), as the same quarter of another year; any other quarter, and a second year
    ("three months ended March 31, 2024 and 2023"), adds None: a period this rule cannot date."""
    import datetime as dt
    out = set()
    for q, y in QUARTER.findall(s):
        try:
            out.add(per.replace(year=int(y)) if per.month == 3 * int(q) else None)
        except ValueError:                                      # February 29 in a year without one
            out.add(None)
    for mo, d, y, y2 in ENDED.findall(s):
        out.add(dt.datetime.strptime(f"{mo.title()} {d} {y}", "%B %d %Y").date())
        if y2:
            out.add(None)
    return out


def listed(seen: set, raw_seen: set, url: str, raw: str, kind: str, place: int, sentence: str) -> bool:
    """Does this mention get its own row? A sentence: once per paragraph (place = where its paragraph starts), so the same
    words printed again in another paragraph (a 10-Q reprinting last year's text) are listed again, and the name twice in
    one sentence is listed once. Every other kind (hidden data, a heading, a cell, a list line): once per text in a filing
    (raw = the scan's text of the mention), as the scan has always listed them. raw_seen and seen are updated here."""
    first = (url, raw) not in raw_seen
    raw_seen.add((url, raw))
    if kind != "sentence" and not first:
        return False
    k = (url, place, kind, sentence)
    if kind != "hidden" and k in seen:
        return False
    seen.add(k)
    return True


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", s)).replace("\xa0", " ").replace("​", "").replace("‌", ""))


class Doc:
    """One filing: where its blocks, tables and hidden parts are."""

    def __init__(self, t: str):
        self.t = t
        self._closers: dict = {}
        hid = [(m.start(), m.end()) for m in HIDDEN.finditer(t)] + self._elements(NONE.finditer(t))
        self.hid, self.hstart = merged(hid)
        tiny = self._elements(m for m in STYLED.finditer(t) if tiny_font(m[2]))
        self.tiny, self.tstart = merged(tiny)
        bl = list(BLOCK.finditer(t))
        self.bstart, self.bend = [b.start() for b in bl], [b.end() for b in bl]
        self.cells = [(m.start(), m.end(), m[1] == "") for m in re.finditer(r"<(/?)t[dh]\b[^>]*>", t, re.I)]
        self.cstart = [c[0] for c in self.cells]
        self._joins()

    def _elements(self, starts) -> list:
        """(start, end) of each element whose start tag matched, up to its matching close tag. An element inside one
        already found is part of it."""
        out, done = [], 0
        for m in starts:
            if m.start() < done:
                continue
            tag, depth, end = m[1].lower(), 1, m.end()                # no close tag found: only the tag itself
            if m[0].rstrip(">").rstrip().endswith("/") or tag in VOID:  # <td style="display:none"/> holds nothing
                out.append((m.start(), m.end()))
                continue
            pat = self._closers.setdefault(tag, re.compile(rf"<(/?){re.escape(tag)}\b[^>]*?(/?)>", re.I))
            for x in pat.finditer(self.t, m.end()):
                if x[2]:
                    continue
                depth += -1 if x[1] else 1
                if depth == 0:
                    end = x.end()
                    break
            out.append((m.start(), end))
            done = end
        return out

    def _seg(self, k: int) -> tuple:
        """(start, end, text) of the text between block tag k-1 and block tag k."""
        a = self.bend[k - 1] if k > 0 else 0
        b = self.bstart[k] if k < len(self.bstart) else len(self.t)
        return a, b, clean(self.t[a:b]).strip()

    def _content(self, k: int, step: int, furn: set):
        """The first text block from block k on (step +1) or back (step -1) that is not page furniture; None if none
        within 40 blocks."""
        for _ in range(40):
            if k < 0 or k > len(self.bstart):
                return None
            a, b, txt = self._seg(k)
            if txt and not FURNITURE.fullmatch(txt) and re.sub(r"\d+", "#", txt.lower()) not in furn and not self.hidden(a + (b - a) // 2):
                return a, b, txt
            k += step
        return None

    def _joins(self) -> None:
        """Where a sentence runs on across a page break. Around each break (<hr>, page-break-before/after) the text blocks
        that are only page furniture (empty, a page number, "Table of Contents", a running header or footer that follows
        at least 3 breaks) are skipped; the last text before the break and the first text after it are joined when the
        first does not end a sentence and the second carries it on (it starts in lower case, or the first ends with a
        comma or a word like 'the', 'of', 'to', 'Mr.')."""
        self.join_prev, self.join_next = {}, {}
        marks = [bisect.bisect_left(self.bstart, m.start()) for m in PAGE_BREAK.finditer(self.t)]
        if not marks:
            return
        import collections
        seen = collections.Counter()
        for q in marks:                                         # running headers and footers: the same short text at many breaks
            for x in (self._content(q + 1, +1, set()), self._content(q, -1, set())):
                if x and len(x[2]) <= 120 and not TERMINAL.search(x[2]):
                    seen[re.sub(r"\d+", "#", x[2].lower())] += 1
        furn = {x for x, c in seen.items() if c >= 3}
        for q in marks:
            p_, n_ = self._content(q, -1, furn), self._content(q + 1, +1, furn)
            if not p_ or not n_ or p_[1] > n_[0]:
                continue
            if TERMINAL.search(p_[2]) or not (n_[2][:1].islower() or CONNECTOR.search(p_[2])):
                continue
            self.join_prev[n_[0]] = (p_[0], p_[1])
            self.join_next[p_[1]] = (n_[0], n_[1])

    def hidden(self, pos: int) -> bool:
        i = bisect.bisect_right(self.hstart, pos) - 1
        if i >= 0 and self.hid[i][0] <= pos < self.hid[i][1]:
            return True
        lt, gt = self.t.rfind("<", 0, pos), self.t.rfind(">", 0, pos)
        return lt > gt and re.match(r"</?[A-Za-z!?]", self.t[lt:lt + 3]) is not None    # inside a tag

    def in_cell(self, pos: int) -> bool:
        i = bisect.bisect_right(self.cstart, pos) - 1
        return i >= 0 and self.cells[i][2]                          # the last cell tag before the name opens a cell

    def block(self, pos: int) -> tuple:
        """(text before the name, text from the name on) inside the name's own block, with the text it runs on from or
        to across a page break (page furniture left out)."""
        i = bisect.bisect_right(self.bend, pos)
        a = self.bend[i - 1] if i else 0
        j = bisect.bisect_left(self.bstart, pos)
        b = self.bstart[j] if j < len(self.bstart) else len(self.t)
        before, after = clean(self.t[a:pos]).lstrip(), clean(self.t[pos:b]).rstrip()
        if a in self.join_prev:
            pa, pb = self.join_prev[a]
            before = clean(self.t[pa:pb]).strip() + " " + before
        if b in self.join_next:
            na, nb = self.join_next[b]
            after = after + " " + clean(self.t[na:nb]).strip()
        return before, after

    def after_bullet(self, pos: int) -> bool:
        """The text just before the name's block is only a bullet or dash mark, alone in its own element."""
        i = bisect.bisect_right(self.bend, pos)
        a = self.bend[i - 1] if i else 0
        s = re.sub(r"(?:\s*<[^>]*>)+\s*$", "", self.t[max(0, a - 600):a])     # the tags between the mark and the block
        gt = s.rfind(">")
        return gt >= 0 and bool(re.fullmatch(r"[●•▪◦·○■□►➢\-–—]", html.unescape(s[gt + 1:]).replace("\xa0", " ").strip()))

    def named_period(self, pos: int, sent: str, period: str) -> str:
        """The period a sentence is about, when the filing's own words say so: the sentence, the part of its paragraph
        before it, or one of the two paragraphs just above it (a heading ends the search; the nearest text that names a
        period decides) names exactly one period ("Q1 2024", "three months ended March 31, 2024"), earlier than the
        filing's period and at most two years before it. A 10-Q prints last year's paragraphs again next to this year's
        ("...profitable in Q1 2024 ..."); their sentences belong to last year's period. "" otherwise: the filing's own
        period applies (none named, two named, or a quarter that cannot be dated against this filing)."""
        per = to_date(period)
        if not per or not sent:
            return ""
        before, after = self.block(pos)
        full = before + after
        k = full.find(sent[:60])
        texts = [sent, full[:k] if k > 0 else ""]
        i, n = bisect.bisect_right(self.bend, pos) - 1, 0
        while i > 0 and n < 2:
            a, b, txt = self._seg(i)
            i -= 1
            if not txt or self.hidden(a + (b - a) // 2) or FURNITURE.fullmatch(txt):
                continue
            if not is_sentence(txt):
                break                                           # a heading: the section starts here
            texts.append(txt)
            n += 1
        for s in texts:
            ds = named_periods(s, per)
            if ds:
                d = ds.pop() if len(ds) == 1 else None
                if d and d < per and (per - d).days <= 800:
                    return d.isoformat() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", period.strip()) else f"{d:%B} {d.day}, {d.year}"
                return ""
        return ""

    def para_start(self, pos: int) -> int:
        """Where the paragraph that holds pos starts: its block, or, for the second half of a sentence joined across a page
        break, the block of its first half (so the name in both halves is still one sentence)."""
        a = self.block_start(pos)
        for _ in range(5):
            if a not in self.join_prev:
                break
            a = self.join_prev[a][0]
        return a

    def block_start(self, pos: int) -> int:
        """Where the text block that holds pos starts (a text layer too small to read: where that element starts)."""
        small = self.tiny_text(pos)
        if small:
            return small[0]
        i = bisect.bisect_right(self.bend, pos)
        return self.bend[i - 1] if i else 0

    def tiny_text(self, pos: int):
        """(start, end) of the too-small-to-read element that holds pos, or None."""
        i = bisect.bisect_right(self.tstart, pos) - 1
        if i < 0 or not self.tiny[i][0] <= pos < self.tiny[i][1]:
            return None
        a, b = self.tiny[i]
        for m in reversed(list(STYLED.finditer(self.t, a + 1, pos))):    # an element inside that sets a readable size again
            (x, y), = self._elements([m]) or [(m.start(), m.end())]
            if x <= pos < y:
                return None if not tiny_font(m[2]) else (a, b)
        return a, b

    def mention(self, pos: int, end: int) -> dict:
        """{"kind", "sentence", "block"} for the name at [pos, end)."""
        if self.hidden(pos):
            return {"kind": "hidden", "sentence": "", "block": ""}
        small = self.tiny_text(pos)
        if small:                                               # a text layer too small to read: the words around the name
            a, b = small
            before, after = clean(self.t[a:pos]), clean(self.t[pos:b])
            words = (("… " if len(before) > 200 else "") + before[-200:].lstrip() + after[:250].rstrip() + (" …" if len(after) > 250 else "")).strip()
            img = None
            for x in re.finditer(r"<img\b[^>]*>", self.t[max(0, a - 3000):a], re.I):
                img = x
            alt = re.search(r"\b(?:alt|title)\s*=\s*[\"']([^\"']+)[\"']", img[0], re.I) if img else None
            size = FONT_SIZE.search(re.search(r"<[^>]*>", self.t[a:b])[0]) if re.search(r"<[^>]*>", self.t[a:b]) else None
            where = f"under the picture “{alt[1]}”" if alt else "on the page"
            return {"kind": "hidden text", "sentence": words,
                    "block": f"Text printed {where} in a font too small to read ({size[1] + (size[2] or '') if size else 'tiny'}); a reader sees only the picture. Words around the name: {words}"}
        before, after = self.block(pos)
        name = clean(self.t[pos:end])
        full, i = before + after, len(before)                  # the whole block, so a sentence end right before the name is seen
        sent = S.sentence_at(full, i, i + (len(name) if after.startswith(name) else 0))
        kind = "sentence" if is_sentence(sent) else "table cell" if self.in_cell(pos) else "heading or label"
        if kind == "sentence" and sent.strip() == full.strip() and len(sent) < 200 and self.after_bullet(pos):
            kind = "list item"                                  # "–" in its own box, the line in the next (a list drawn as boxes)
        if len(full) > 4000:                                    # a very long block: the 4,000 characters around the name
            blk = ("…" if i > 2000 else "") + full[max(0, i - 2000):i + 2000] + ("…" if i + 2000 < len(full) else "")
        else:
            blk = full
        return {"kind": kind, "sentence": sent[:1500], "block": blk.strip()}


def merged(spans: list) -> tuple:
    """Spans sorted and merged, so one bisect answers "inside?"; (spans, their starts)."""
    out: list = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(b, out[-1][1]))
        else:
            out.append((a, b))
    return out, [a for a, _ in out]


def tiny_font(style: str) -> bool:
    """A font size no one can read: 1pt or 1px or less, or 0 in any unit."""
    m = FONT_SIZE.search(style)
    if not m:
        return False
    v, unit = float(m[1]), (m[2] or "").lower()
    return v == 0 or (unit in ("pt", "px", "") and v <= 1)


def is_sentence(s: str) -> bool:
    """Running text: at least six words, not mostly numbers, and either mostly lower-case words or a full stop at the
    end with some lower-case words (a sentence that lists many names is still a sentence)."""
    words = WORD.findall(s)
    if len(words) < 6:
        return False
    lower = sum(1 for w in words if w[0].islower())
    nums = len(re.findall(r"\d[\d,.]*", s))
    if nums > 0.5 * len(words):
        return False
    return lower >= 0.4 * len(words) or (lower >= 3 and bool(re.search(r"[.!?][\"”’)]?$", s.strip())))


def junk(s: str) -> str:
    """Why a sentence is not clean text ("" when it is clean). A check on the output, used by the tests."""
    if re.search(r"<[a-zA-Z/!]|\b(?:font-[a-z]+|margin[a-z-]*|padding[a-z-]*|text-[a-z]+|line-height|color|width|display)\s*:\s*[^;\"]{1,40};|\b(?:style|class|colspan|href|id)\s*=", s):
        return "HTML or CSS"
    if re.search(r"\b[a-z][a-z0-9-]*:[A-Z]\w+(?:Member|Axis|Domain)\b|\b\d{10}\b.*\b\d{10}\b", s):
        return "XBRL data"
    if not is_sentence(s):
        return "not a sentence"
    return ""


def main() -> None:
    """Rewrite out/<co>/evidence/prose_sentences.csv: each mention the table readers did not take is read again from the
    filing at its position and given a kind (sentence / hidden / table cell / heading or label / pdf line) and its whole
    block. The scan's own text is kept in prose_sentences_raw.csv."""
    import csv
    import shutil
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import paths as px
    csv.field_size_limit(10**9)
    co = sys.argv[1]
    cfg = __import__("json").loads(px.config_path(co).read_text(encoding="utf-8"))
    key = re.compile(cfg["company"]["name_regex"], re.I)
    ev = px.OUT / co / "evidence"
    src, raw = ev / "prose_sentences.csv", ev / "prose_sentences_raw.csv"
    rd = csv.DictReader(open(src, encoding="utf-8"))
    rows, fields = list(rd), list(rd.fieldnames or [])
    if "kind" in fields:                                # already rewritten: read the scan's text again
        rd = csv.DictReader(open(raw, encoding="utf-8"))
        rows, fields = list(rd), list(rd.fieldnames or [])
    else:
        shutil.copy(src, raw)
    fields = [f for f in fields if f not in ("kind", "block", "period_named")] + ["kind", "block", "period_named"]
    paths = {r["sec_url"]: px.DUMPS / m.name.replace("_manifest.csv", "") / r["file"] for m in px.DUMPS.glob(f"{co}*_manifest.csv")
             for r in csv.DictReader(open(m, encoding="utf-8"))}
    out, cur, doc, seen, raw_seen = [], None, None, set(), set()
    import collections
    n = collections.Counter()
    for r in sorted(rows, key=lambda r: (r["url"], int(r["pos"]) if r["pos"].isdigit() else 0)):
        p = paths.get(r["url"])
        if not p or not p.exists() or p.suffix.lower() not in (".htm", ".html") or not r["pos"].isdigit():
            k = "pdf line" if is_sentence(r["sentence"]) else "pdf table line"     # a PDF has no tags: the line is read as extracted
            out.append({**r, "kind": k, "block": r["sentence"]})
            n[k] += 1
            continue
        if r["url"] != cur:
            t = p.read_text(encoding="utf-8", errors="ignore")
            t = re.sub(r"</?(?:table|tr|td|th)\b", lambda x: x[0].lower(), t, flags=re.I)   # same text the scan read
            cur, doc = r["url"], Doc(t)
        pos = int(r["pos"])
        m = key.match(doc.t, pos)
        if not m:
            raise SystemExit(f"position {pos} of {r['url']} does not hold the name: the filing changed since the scan")
        x = doc.mention(pos, m.end())
        place = doc.para_start(pos) if x["kind"] == "sentence" else doc.block_start(pos)
        if not listed(seen, raw_seen, r["url"], r["sentence"], x["kind"], place, x["sentence"]):
            n["same words in the same paragraph (or same text, not a sentence): listed once"] += 1
            continue
        n[x["kind"]] += 1
        out.append({**r, "sentence": x["sentence"], "kind": x["kind"], "block": x["block"],
                    "period_named": doc.named_period(pos, x["sentence"], r.get("period", "")) if x["kind"] == "sentence" else ""})
    w = csv.DictWriter(open(src, "w", newline="", encoding="utf-8"), fields)
    w.writeheader()
    w.writerows(out)
    print("prose rows by kind:", dict(n))


if __name__ == "__main__":
    main()
