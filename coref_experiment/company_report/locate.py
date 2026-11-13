#!/usr/bin/env python3
"""Where to look in the filing for each verified row: the page (counted from the filing's own page breaks, or the PDF
page) and the exact text to search for (Ctrl+F) - a number as printed in that row, unique enough to land on it.

    python locate.py <company>      -> out/<co>/evidence/locator.csv  (id, page, search_text, where)
"""
from __future__ import annotations

import collections
import csv
import html
import re
import sys
from pathlib import Path

csv.field_size_limit(10**9)
co = sys.argv[1]
HERE = Path(__file__).parent
ROOT = HERE.parents[2]
ev = HERE / "out" / co / "evidence"
E = {r["evidence_id"]: r for r in csv.DictReader(open(ev / "evidence_rows.csv", encoding="utf-8"))}
H = list(csv.DictReader(open(ev / "holdings_from_evidence.csv", encoding="utf-8")))
F = list(csv.DictReader(open(ev / "facts_from_evidence.csv", encoding="utf-8")))
P = collections.defaultdict(list)
for p in csv.DictReader(open(ev / "prose_sentences.csv", encoding="utf-8")):
    P[p["accession"]].append(p)
local = collections.defaultdict(list)
for man in sorted((ROOT / "dumps").glob(f"{co}*_manifest.csv")):
    f = man.name.replace("_manifest.csv", "")
    for r in csv.DictReader(open(man, encoding="utf-8")):
        local[r["accession"]].append(ROOT / "dumps" / f / r["file"])
from pages import BREAK, FOOT, breaks_of, page_at, txt   # noqa: E402,F401


def printed_number(raw: str, vals: list) -> str:
    """The longest number printed in the row that equals one of the row's values (as printed, with commas)."""
    cells = re.findall(r"\(?\$?\s*\d[\d,]*(?:\.\d+)?\s*\)?", raw)
    best = ""
    for c in cells:
        n = re.sub(r"[^\d.]", "", c)
        try:
            v = float(n)
        except ValueError:
            continue
        if any(abs(abs(x) - v) < 0.6 for x in vals if x is not None) and len(c.strip()) > len(best):
            best = c.strip(" ()$")
    if len(best) < 5:                                  # a short number repeats: take the longest number in the row
        longest = max((c.strip(" ()$") for c in cells), key=len, default="")
        best = longest if len(longest) > len(best) else best
    return best


rows = {}
need = collections.defaultdict(list)
for h in H:
    need[E[h["evidence_id"]]["accession"]].append(("h", h))
for f in F:
    need[f["accession"]].append(("f", f))
held = {h["evidence_id"] for h in H}
for r in E.values():                                   # every other evidence row too: held back, repeated, not a position
    if r["evidence_id"] not in held:
        need[r["accession"]].append(("h", {"evidence_id": r["evidence_id"]}))
for acc_, ps in P.items():                             # and every sentence that names the company
    for p_ in ps:
        need[acc_].append(("p", p_))
import json
NAME = re.compile(json.loads((HERE / "model_config" / f"{co}.json").read_text(encoding="utf-8"))["company"]["name_regex"], re.I)


def prefix_of(raw: str, needle: str) -> str:
    """The text printed just before the search text in the same cell, so a link lands on this row even when the number
    appears elsewhere in the filing. Only the same cell: a browser matches a prefix only inside the block that holds the
    text, so the end of the cell before would make the link fail."""
    cells = [c.strip() for c in raw.split("|")]
    for c in cells:
        if needle and needle in c:
            before = c[:c.find(needle)].strip(" $(")
            return " ".join(before.split()[-3:]) if before else ""
    return ""


def name_candidates(text: str) -> list:
    """Words around the company name as printed, longest first. The word that holds the name is kept whole: a name inside a
    longer word ('GalaxyPlusFundHorizon3FeederFund577LLCMember') is never split by added spaces."""
    m = NAME.search(text)
    if not m:
        return [text[:40]]
    s = text.rfind(" ", 0, m.start()) + 1
    e = text.find(" ", m.end())
    e = len(text) if e < 0 else e
    a, word, b = text[:s].split(), text[s:e], text[e:].split()
    out = []
    for na, nb in ((3, 2), (2, 1), (1, 1), (0, 1), (1, 0), (0, 0)):
        c = " ".join((a[-na:] if na else []) + [word] + b[:nb]).strip(" ,;:")
        if c and c not in out:
            out.append(c)
    return out


def name_words(text: str) -> str:
    """A few words around the company name, as printed, to search for."""
    return name_candidates(text)[0]


HIDDEN = re.compile(r"(?is)<ix:header\b.*?</ix:header>|<head\b.*?</head>|<(script|style)\b.*?</\1>")
VBLOCK = re.compile(r"<(?:/?(?:p|div|tr|td|th|li|h[1-6]|table|center|section|blockquote|pre)\b[^>]*|br\s*/?)>", re.I)


def blocks_of(t: str) -> list:
    """The text of each block a browser shows (paragraph, table cell, line), hidden XBRL data left out. A link highlights
    its text only when the text is printed inside one of these blocks."""
    out = []
    for part in VBLOCK.sub("\x00", HIDDEN.sub(" ", t)).split("\x00"):
        s = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", part)).replace("\xa0", " ").replace("\u200b", "")).strip()
        if s:
            out.append(s)
    return out


def fit(cands: list, blocks: list) -> str:
    """The first candidate printed inside one block, so a browser can highlight it; '' (the link just opens the filing) when none is."""
    for c in cands:
        c = re.sub(r"\s+", " ", c or "").strip()
        if c and any(c in b for b in blocks):
            return c
    return ""
for n, (acc, items) in enumerate(sorted(need.items())):
    paths = [p for p in local.get(acc, []) if p.exists()]
    cache = {}

    def text_of(p):
        if p not in cache:
            cache.clear()
            t = re.sub(r"</?(?:table|tr|td|th)\b", lambda x: x[0].lower(), p.read_text(encoding="utf-8", errors="ignore"), flags=re.I)
            cache[p] = (t, breaks_of(t))
            cache["plain"] = txt(t)
            cache["blocks"] = blocks_of(t)
        return cache[p]
    for kind, it in items:
        if kind == "h":
            e = E[it["evidence_id"]]
            vals = [float(x) for x in (e["qty"], e["cost"], e["value"], e["commitment"]) if x]
            res = {"id": it["evidence_id"], "page": "", "search_text": "", "where": ""}
            if e["method"] == "pdf line":
                res.update(page=e["table"].replace("page ", ""), search_text=printed_number(e["raw"], vals), where="PDF page")
            elif e["method"] == "N-CEN XML":
                res.update(page="-", search_text="principalTransaction", where="XML section principalTransactions")
            else:
                p = next((x for x in paths if x.suffix.lower() in (".htm", ".html")), None)
                if p:
                    t, br = text_of(p)
                    if e["method"] == "iXBRL tag":
                        m = re.search(rf'<ix:nonFraction[^>]*contextRef="{re.escape(e["table"])}"[^>]*name="[^"]*FairValue[^"]*"[^>]*>(.*?)</ix:nonFraction>|'
                                      rf'<ix:nonFraction[^>]*name="[^"]*FairValue[^"]*"[^>]*contextRef="{re.escape(e["table"])}"[^>]*>(.*?)</ix:nonFraction>', t, re.S)
                        m = m or re.search(rf'<ix:nonFraction[^>]*contextRef="{re.escape(e["table"])}"[^>]*>(.*?)</ix:nonFraction>', t, re.S)
                        if m:
                            res.update(page=page_at(t, br, m.start()), search_text=txt(m[1] or m[2] or ""), where="schedule of investments row (iXBRL)")
                    else:
                        pos = int(e["table"]) if e["method"] == "table" else int(e["row"])
                        st = printed_number(e["raw"], vals) if vals else ""
                        name = (e.get("name") or e["raw"].split("|")[0]).strip()
                        head = re.split(r"\s*\([a-z0-9]{1,3}\)|[*†‡#]", name)[0].strip()      # the name as printed, up to a footnote mark
                        cut = lambda s: s[:50].rsplit(" ", 1)[0] if len(s) > 50 else s      # noqa: E731
                        st = fit([st, cut(head), cut(name), head, name], cache["blocks"])    # a row without an amount: its printed name
                        res.update(page=page_at(t, br, pos), search_text=st, prefix=prefix_of(e["raw"], st) if vals else "",
                                   where=f"table row {int(str(e['row']).split('-')[0]) + 1}" if e["method"] == "table" else "text block")
            res["times_in_filing"] = cache.get("plain", "").count(res["search_text"]) if res["search_text"] and cache.get("plain") else ""
            rows[it["evidence_id"]] = res
        elif kind == "p":
            snt = re.sub(r'^[^<>]{0,80}?"\s*>\s*', "", it["sentence"])
            res = {"id": f"S{acc}:{it['pos']}", "page": "", "search_text": name_words(snt), "where": "sentence"}
            if str(it["pos"]).startswith("page"):
                res["page"] = it["pos"].replace("page ", "")
            else:
                p = next((x for x in paths if x.suffix.lower() in (".htm", ".html")), None)
                if p and str(it["pos"]).isdigit():
                    t, br = text_of(p)
                    res["page"] = page_at(t, br, int(it["pos"]))
                    res["search_text"] = fit(name_candidates(snt), cache["blocks"])   # '' for hidden XBRL data: the link opens the filing
            res["times_in_filing"] = cache.get("plain", "").count(res["search_text"]) if res["search_text"] and cache.get("plain") else ""
            rows[res["id"]] = res
        else:
            res = {"id": it["fact_id"], "page": "", "search_text": it["printed"].strip(), "where": "sentence"}
            if it["metric"] == "fund_transaction":
                res.update(page="-", search_text="principalTransaction", where="XML section principalTransactions")
            else:
                p_ = next((x for x in P.get(acc, []) if x["sentence"][:200] in it["sentence"] or it["sentence"][:200] in x["sentence"]), None)
                p = next((x for x in paths if x.suffix.lower() in (".htm", ".html")), None)
                if p_ and p and str(p_["pos"]).isdigit():
                    t, br = text_of(p)
                    res["page"] = page_at(t, br, int(p_["pos"]))
                elif p_ and str(p_["pos"]).startswith("page"):
                    res["page"] = p_["pos"].replace("page ", "")
            res["times_in_filing"] = cache.get("plain", "").count(res["search_text"]) if res["search_text"] and cache.get("plain") else ""
            rows[it["fact_id"]] = res
    if n % 200 == 0:
        print("filings", n, "of", len(need), flush=True)
with open(ev / "locator.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, ["id", "page", "search_text", "prefix", "times_in_filing", "where"])
    w.writeheader()
    w.writerows(rows.values())
print(len(rows), "rows located;", sum(1 for r in rows.values() if r["page"]), "with a page;", sum(1 for r in rows.values() if r["search_text"]), "with search text")
