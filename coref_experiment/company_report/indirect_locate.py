#!/usr/bin/env python3
"""Where each indirect mention (it / its / they / the company ...) sits in the filing: the printed page and the words to
search for, so a link opens the filing at the pronoun, highlighted, the same way as the string-match evidence.

    python indirect_locate.py <company> <indirect out dir> [<indirect out dir> ...]
        -> out/<co>/indirect_located.csv

Rows kept: every trigger the model decided (decided_by = llm), and every trigger resolved to the company.
"""
from __future__ import annotations

import csv
import glob
import json
import re
import sys
from pathlib import Path

from pages import load, page_at, txt

csv.field_size_limit(10**9)
co, dirs = sys.argv[1], [Path(x) for x in sys.argv[2:]]
HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "coref_verifier" / "data"
META = {}
for mf in glob.glob(str(DATA / "companies" / f"{co}*__*_mentions.meta.json")):   # also <co>_ncsr__..., <co>_delta__...
    m = json.load(open(mf, encoding="utf-8"))
    META[Path(m["document"]).stem] = m
SEP = r"(?:\W|<[^>]*>|&#?\w+;)+"                       # what can sit between two words in the HTML


def snippet(sentence: str, context: str, start: int, word: str) -> str:
    """About nine words around the trigger, as printed in the sentence."""
    s0 = context.find(sentence)
    off = start - s0 if s0 >= 0 and 0 <= start - s0 < len(sentence) else -1
    if off < 0 or not sentence[off:].lower().startswith(word.lower()):
        m = re.search(rf"\b{re.escape(word)}(?![\w])", sentence)
        off = m.start() if m else 0
    before = sentence[:off].split()[-5:]
    after = sentence[off:].split()[:4]
    return " ".join(before + after).strip(" ,;:(")


def norm(x: str) -> str:
    return re.sub(r"\s+([,.;:)%])", r"\1", re.sub(r"\(\s+", "(", " ".join(x.split())))


rows = []
for d in dirs:
    for f in sorted(d.glob("*_indirect_all_triggers.csv")):
        stem = f.name.replace("_indirect_all_triggers.csv", "")
        m = META.get(stem)
        if not m:
            continue
        keep = [r for r in csv.DictReader(open(f, encoding="utf-8"))
                if r["decided_by"] == "llm" or (r["status"] == "resolved" and r["company_id"].startswith("C"))]
        if not keep:
            continue
        doc = DATA / "pdfs" / m["document"]
        html_ = doc.suffix.lower() in (".htm", ".html") and doc.exists()
        t, br = load(doc) if html_ else ("", [])
        plain = norm(txt(t)) if html_ else ""
        for r in keep:
            sn = snippet(r["sentence"], r["context"], int(r["start"] or 0), r["trigger"])
            page, found, times = (f"{r['page']} (counted)" if html_ else r["page"]), "", ""
            if html_:
                toks = re.findall(r"\w+", sn)
                hit = re.search(r"\b" + SEP.join(map(re.escape, toks)) + r"\b", t, re.S) if toks else None
                if hit:
                    page = page_at(t, br, hit.start())
                found = "yes" if norm(sn) in plain else "no"
                times = plain.count(norm(sn))
            rows.append({"run": d.name, "form": m["form"], "filer": m["filer"], "filed": m["file_date"], "accession": m["accession"],
                         "url": m["sec_url"], "trigger": r["trigger"], "trigger_type": r["trigger_type"], "sentence": r["sentence"],
                         "decided_by": r["decided_by"], "status": r["status"], "company": r["company"], "company_id": r["company_id"],
                         "confidence": r["confidence"], "reason": r["reason"], "page": page, "search_text": sn,
                         "found_in_filing": found, "times_in_filing": times})
out = HERE / "out" / co / "indirect_located.csv"
with open(out, "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, list(rows[0]) if rows else ["run"])
    w.writeheader()
    w.writerows(rows)
h = [r for r in rows if r["found_in_filing"]]
print(out, len(rows), "rows;", sum(1 for r in rows if r["company_id"].startswith("C")), "to the company;",
      f"search text found in the filing: {sum(1 for r in h if r['found_in_filing'] == 'yes')} of {len(h)} html rows")
