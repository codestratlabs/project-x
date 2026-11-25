#!/usr/bin/env python3
"""The sentence that names the company, read from the filing's HTML by its structure (no fixed-width slices).

A mention the table readers did not take is one of:
  hidden        not on a page: inside <ix:header>, <head>, <script>, <style>, an HTML comment, a display:none
                element, or inside a tag (an attribute value)
  sentence      running text: the sentence around the name, cut at sentence ends inside its own block
                (<p>, <div>, <li>, table cell, heading)
  table cell    a cell of a table that is not a sentence (a name, a label, a number)
  heading or label   a block outside tables that is not a sentence

Only whole blocks are read, so no tag or style text can leak into the sentence.
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
NONE = re.compile(r"<([a-z][a-z0-9:]*)\b[^>]*\bstyle\s*=\s*[\"'][^\"']*display\s*:\s*none[^\"']*[\"'][^>]*>", re.I)
WORD = re.compile(r"[A-Za-z][A-Za-z’'-]*")


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", s)).replace("\xa0", " ").replace("​", "").replace("‌", ""))


class Doc:
    """One filing: where its blocks, tables and hidden parts are."""

    def __init__(self, t: str):
        self.t = t
        hid = [(m.start(), m.end()) for m in HIDDEN.finditer(t)]
        done, closers = 0, {}
        for m in NONE.finditer(t):                                  # display:none element: up to its matching close tag
            if m.start() < done:
                continue                                            # inside an element already hidden
            tag, depth, end = m[1].lower(), 1, m.end()                # no close tag found: only the tag itself is hidden
            if m[0].rstrip(">").rstrip().endswith("/") or tag in VOID:  # <td style="display:none"/> holds nothing
                hid.append((m.start(), m.end()))
                continue
            pat = closers.setdefault(tag, re.compile(rf"<(/?){re.escape(tag)}\b[^>]*?(/?)>", re.I))
            for x in pat.finditer(t, m.end()):
                if x[2]:
                    continue
                depth += -1 if x[1] else 1
                if depth == 0:
                    end = x.end()
                    break
            hid.append((m.start(), end))
            done = end
        hid.sort()
        self.hid = []
        for a, b in hid:                                            # merged, so one bisect answers "hidden?"
            if self.hid and a <= self.hid[-1][1]:
                self.hid[-1] = (self.hid[-1][0], max(b, self.hid[-1][1]))
            else:
                self.hid.append((a, b))
        self.hstart = [a for a, _ in self.hid]
        bl = list(BLOCK.finditer(t))
        self.bstart, self.bend = [b.start() for b in bl], [b.end() for b in bl]
        self.cells = [(m.start(), m.end(), m[1] == "") for m in re.finditer(r"<(/?)t[dh]\b[^>]*>", t, re.I)]
        self.cstart = [c[0] for c in self.cells]

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
        """(text before the name, text from the name on) inside the name's own block."""
        i = bisect.bisect_right(self.bend, pos)
        a = self.bend[i - 1] if i else 0
        j = bisect.bisect_left(self.bstart, pos)
        b = self.bstart[j] if j < len(self.bstart) else len(self.t)
        return clean(self.t[a:pos]).lstrip(), clean(self.t[pos:b]).rstrip()

    def mention(self, pos: int, end: int) -> dict:
        """{"kind", "sentence", "block"} for the name at [pos, end)."""
        if self.hidden(pos):
            return {"kind": "hidden", "sentence": "", "block": ""}
        before, after = self.block(pos)
        name = clean(self.t[pos:end])
        full, i = before + after, len(before)                  # the whole block, so a sentence end right before the name is seen
        sent = S.sentence_at(full, i, i + (len(name) if after.startswith(name) else 0))
        kind = "sentence" if is_sentence(sent) else "table cell" if self.in_cell(pos) else "heading or label"
        if len(full) > 4000:                                    # a very long block: the 4,000 characters around the name
            blk = ("…" if i > 2000 else "") + full[max(0, i - 2000):i + 2000] + ("…" if i + 2000 < len(full) else "")
        else:
            blk = full
        return {"kind": kind, "sentence": sent[:1500], "block": blk.strip()}


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
    fields = [f for f in fields if f not in ("kind", "block")] + ["kind", "block"]
    paths = {r["sec_url"]: px.DUMPS / m.name.replace("_manifest.csv", "") / r["file"] for m in px.DUMPS.glob(f"{co}*_manifest.csv")
             for r in csv.DictReader(open(m, encoding="utf-8"))}
    out, cur, doc = [], None, None
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
        n[x["kind"]] += 1
        out.append({**r, "sentence": x["sentence"], "kind": x["kind"], "block": x["block"]})
    w = csv.DictWriter(open(src, "w", newline="", encoding="utf-8"), fields)
    w.writeheader()
    w.writerows(out)
    print("prose rows by kind:", dict(n))


if __name__ == "__main__":
    main()
