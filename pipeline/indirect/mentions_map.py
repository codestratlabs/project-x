"""The company-finder mentions of one filing, mapped to the filing's paragraphs: which listed company is named in which
paragraph (load_mentions, locate), which company filed the document (pick_filer), and whether a paragraph is a table.
"""
from __future__ import annotations

import ast
import collections
import csv
import json
import re
from pathlib import Path

import text_llm as c

csv.field_size_limit(10**8)


def _extras(v):
    if isinstance(v, dict):
        return v
    for f in (json.loads, ast.literal_eval):
        try:
            out = f(v)
            return out if isinstance(out, dict) else {}
        except Exception:  # noqa: BLE001
            pass
    return {}


def load_mentions(path: str, pdf_name: str) -> list[dict]:
    p = Path(path)
    if p.suffix.lower() == ".json":
        rows = json.loads(p.read_text(encoding="utf-8")).get("mentions", [])
    else:
        rows = list(csv.DictReader(open(p, encoding="utf-8")))
    out = []
    for r in rows:
        if r.get("document") and Path(str(r["document"])).name != pdf_name:
            continue
        ex = _extras(r.get("extras"))
        if ex.get("llm_disambiguation", {}).get("is_company") is False:
            continue  # defensive: a dropped row in a matching-only file
        out.append({"company_id": str(r.get("company_id")), "name": r.get("canonical_name") or r.get("company_name"),
                    "text": re.sub(r"\s+", " ", r.get("matched_text") or "").strip(), "page": int(float(r.get("page") or r.get("page_number") or 0)),
                    "passage": r.get("passage") or "", "context": r.get("context") or "",
                    "is_filer": str(r.get("is_filer")).lower() == "true"})
    return out


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def has_word(text: str, word: str) -> bool:
    return bool(word) and bool(re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text, re.I))


def _snippet(m: dict) -> str:
    """A short piece of the mention's own sentence around the matched word (for a document-wide search)."""
    ctx = norm(m.get("context") or m.get("passage") or "").lstrip("… ").rstrip(" …")
    i = ctx.lower().find((m["text"] or "").lower()) if m["text"] else -1
    if i < 0:
        return ctx[:60]
    return ctx[max(0, i - 30): i + len(m["text"]) + 30]


def locate(mentions: list[dict], paras: list[c.Para]) -> tuple[dict[int, dict[str, str]], int]:
    """Map each mention to a document paragraph. Returns ({pid: {company_id: name}}, unmapped_count).
    First on the page the pipeline reported (PDFs). If that fails (HTML filings: the pipeline's page numbers do not
    match the pages the viewer lays out), search the whole document for the mention's own sentence; when the same
    sentence appears several times, the mentions are assigned to its occurrences in page order."""
    by_page = collections.defaultdict(list)
    for p in paras:
        by_page[p.page].append(p)
    found: dict[int, dict[str, str]] = collections.defaultdict(dict)
    unmapped, later = 0, []
    for m in mentions:
        cands = by_page.get(m["page"], [])
        hit = None
        key = norm(m["passage"])[:40]
        if len(key) >= 20:
            hit = next((p for p in cands if key in norm(p.text)), None)
        if hit is None and m["text"]:
            hit = next((p for p in cands if has_word(p.text, m["text"])), None)
        if hit is None:
            later.append(m)
            continue
        found[hit.pid][m["company_id"]] = m["name"]
    used: dict[str, int] = collections.defaultdict(int)
    for m in sorted(later, key=lambda x: x["page"]):
        snip = _snippet(m).lower()
        hits = [p for p in paras if len(snip) >= 20 and snip in norm(p.text).lower()
                and (not m["text"] or has_word(p.text, m["text"]))]
        key = f"{m['company_id']}|{snip}"
        if hits:
            hit = hits[min(used[key], len(hits) - 1)]
            used[key] += 1
        else:
            # last resort: the sentence spans several short paragraphs (table cells, lists): take the paragraph that
            # contains the matched word, nearest to the page the pipeline reported
            word_hits = [p for p in paras if m["text"] and has_word(p.text, m["text"])]
            if not word_hits:
                unmapped += 1
                continue
            hit = min(word_hits, key=lambda p: (abs(p.page - m["page"]), p.pid))
        found[hit.pid][m["company_id"]] = m["name"]
    return found, unmapped


def looks_like_table(text: str) -> bool:
    chars = [ch for ch in text if not ch.isspace()]
    return bool(chars) and sum(ch.isdigit() or ch in "$%,." for ch in chars) / len(chars) > 0.35


def pick_filer(mentions: list[dict], override: str) -> tuple[str, str]:
    if override:
        for m in mentions:
            if override.lower() in (m["name"] or "").lower():
                return m["company_id"], m["name"]
        return "F0", override
    flagged = collections.Counter((m["company_id"], m["name"]) for m in mentions if m["is_filer"])
    if flagged:
        return flagged.most_common(1)[0][0]
    first = collections.Counter((m["company_id"], m["name"]) for m in mentions if m["page"] <= 3)
    return first.most_common(1)[0][0] if first else ("F0", "UNKNOWN FILER")


# ---------------------------------------------------------------- candidates and prompt
