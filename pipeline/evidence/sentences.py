"""Sentence boundaries in filing text, with no model.

A full stop ends a sentence only when the word before it is not an abbreviation. Titles ("Mr.", "Dr."), initials
("Michael L. Favet") and multi-dot abbreviations ("Ph.D.", "U.S.") never end a sentence by themselves. Company suffixes
("Inc.", "Corp.", "Ltd.") and "U.S." end one only when the next word plainly starts a sentence ("... Lungpacer, Inc. The
Company ..."), because in lists of companies ("Othera Pharmaceuticals Inc. Intact Vascular, Inc.") they do not.
A semicolon never ends a sentence: it joins clauses or list items of one sentence. A bullet starts a new item.
"""
from __future__ import annotations

import re

# Never a sentence end on their own (the next word is the rest of a name or a phrase).
TITLES = {"mr", "mrs", "ms", "messrs", "mmes", "dr", "drs", "prof", "hon", "rev", "sr", "jr", "st", "gen", "col", "capt", "lt", "sgt",
          "gov", "sen", "rep", "pres", "supt", "fr", "esq"}
OTHER = {"no", "nos", "vol", "vols", "fig", "figs", "sec", "secs", "art", "ch", "para", "pp", "approx", "est", "dept", "e.g", "i.e", "cf",
         "vs", "viz", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "ave", "blvd", "rd", "ste",
         "mt", "ft", "dist", "div", "u.s.a", "a.m", "p.m"}
# End a sentence only when the next word starts one.
SUFFIXES = {"inc", "corp", "co", "cos", "ltd", "llc", "l.l.c", "lp", "l.p", "llp", "l.l.p", "n.a", "s.a", "n.v", "b.v", "plc", "ag",
            "gmbh", "bros", "assn", "u.s", "u.k", "etc", "ph.d", "m.d", "j.d", "m.b.a", "b.s", "b.a", "m.s", "m.a", "c.p.a"}
STARTERS = {"the", "a", "an", "we", "our", "us", "it", "its", "he", "she", "his", "her", "they", "their", "this", "these", "those",
            "that", "there", "in", "on", "as", "at", "for", "from", "since", "prior", "before", "after", "during", "if", "on", "under",
            "pursuant", "accordingly", "however", "additionally", "also", "each", "all", "no", "such", "neither", "none", "any",
            "mr", "mrs", "ms", "dr", "see", "item", "note", "following", "upon", "while", "when", "although", "because", "both"}

BOUNDARY = re.compile(r"([.!?])([\"”’')\]]*)(\s+)(?=[\"“(]?[A-Z0-9•●▪◦])|\s*([•●▪◦])\s*")
WORD_BEFORE = re.compile(r"([A-Za-z][A-Za-z.]*)$")
NEXT_WORD = re.compile(r"[\"“(]?([A-Za-z]+)")


def _is_end(text: str, m: re.Match) -> bool:
    """Is the punctuation of match m a sentence end?"""
    if m[4]:                                          # a bullet: a new list item
        return True
    if m[1] in "!?":
        return True
    w = WORD_BEFORE.search(text, max(0, m.start() - 40), m.start())
    if not w or m[2]:                                 # after a number or a closing quote / bracket: a real end
        return True
    tok = w[1].lower().rstrip(".")
    nxt = NEXT_WORD.match(text, m.end())
    starter = bool(nxt) and nxt[1].lower() in STARTERS
    if tok in TITLES or tok in OTHER:
        return False
    if len(tok) == 1 and w[1][0].isupper():           # an initial ("L.") or a class letter ("Class A.")
        return starter and len(w[1]) == 1 and bool(re.search(r"\b(?:class|series|plan|exhibit|annex|schedule|appendix)\s+$", text[max(0, w.start() - 12):w.start()], re.I))
    if tok in SUFFIXES or "." in tok:                 # "Inc.", "U.S.", "Ph.D.": an end only before a sentence start
        return starter
    return True


def bounds(text: str, pos: int, end: int | None = None) -> tuple[int, int]:
    """(start, stop) of the sentence that holds text[pos:end]."""
    end = pos if end is None else end
    a, b = 0, len(text)
    for m in BOUNDARY.finditer(text):
        if not _is_end(text, m):
            continue
        cut_end = m.start() + len(m[1] or "") + len(m[2] or "") if not m[4] else m.start()
        if m.end() <= pos:
            a = m.end() if not m[4] else m.start() + m[0].index(m[4])
        elif cut_end >= end:
            b = cut_end
            break
    return a, b


def sentence_at(text: str, pos: int, end: int | None = None) -> str:
    a, b = bounds(text, pos, end)
    return text[a:b].strip()


def split(text: str) -> list[str]:
    """Every sentence of text, in order."""
    out, a = [], 0
    for m in BOUNDARY.finditer(text):
        if not _is_end(text, m):
            continue
        stop = m.start() + len(m[1] or "") + len(m[2] or "") if not m[4] else m.start()
        if text[a:stop].strip():
            out.append(text[a:stop].strip())
        a = m.end() if not m[4] else m.start() + m[0].index(m[4])
    if text[a:].strip():
        out.append(text[a:].strip())
    return out
