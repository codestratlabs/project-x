"""Pieces of the indirect step: the trigger words, the sentence around a word, and the scope of one filing (its paragraphs,
the listed companies named in each paragraph, the filer). Only the companies in the filing's company-finder mentions are used.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # indirect/: text_llm.py, mentions_map.py
sys.path.insert(0, str(HERE.parent))  # pipeline/: paths.py

import mentions_map as d  # noqa: E402  (load_mentions, locate, pick_filer, looks_like_table)
import text_llm as c  # noqa: E402
import paths as P  # noqa: E402

OUT_DIR = P.OUT / "indirect"
c.MIN_PARA_CHARS = 1  # keep tiny text blocks (headings like 'Our Mission') so every trigger word is accounted for

# ------------------------------------------------------------------ trigger words (indirect mentions)
TRIGGER_RX = {
    "first_person": re.compile(r"\b(we|our|ours|us|ourselves)\b", re.I),
    "third_singular": re.compile(r"\b(it|its|itself)\b", re.I),
    "third_plural": re.compile(r"\b(they|their|theirs|them|themselves)\b", re.I),
    # a defined term is capitalised ("the Company"); lower-case "the corporation" is generic legal text
    "defined_term": re.compile(r"\b[Tt]he (Company|Fund|Issuer|Registrant|Partnership|Trust|Corporation)\b"),
    # lower-case noun phrases that point back to a named company ("public information about the company's operating
    # history", "such issuer"); case-sensitive so the capitalised defined terms above stay with the filer
    "third_noun": re.compile(r"\b(?:[Tt]he|[Ss]uch|[Tt]his|[Tt]hat) (?:company|issuer|firm|startup)(?:['’]s)?(?![\w-])"),
}
FILER_RULE_TYPES = {"first_person", "defined_term"}   # resolved by rule to the filer
LLM_TYPES = {"third_singular", "third_plural", "third_noun"}   # need the text to decide


def find_triggers(text: str) -> list[dict]:
    """Every trigger word in `text`: {type, word, start, end}. All-capital tokens (US, IT, WE in headings) are skipped."""
    out = []
    for ttype, rx in TRIGGER_RX.items():
        for m in rx.finditer(text):
            w = m.group(0)
            if len(w) > 1 and w.isupper():
                continue
            out.append({"type": ttype, "word": w, "start": m.start(), "end": m.end()})
    return sorted(out, key=lambda t: t["start"])


SENT_SPLIT = re.compile(r"(?<=[.!?;])\s+(?=[A-Z“\"(•])")


def sentence_at(text: str, pos: int) -> str:
    start = 0
    for m in SENT_SPLIT.finditer(text):
        if m.end() > pos:
            return text[start:m.start() + 1].strip()
        start = m.end()
    return text[start:].strip()


# ------------------------------------------------------------------ scope
@dataclass
class Scope:
    pdf: str
    paras: list
    by_id: dict
    filer_id: str                    # e.g. "376-ambiq" (pipeline company id)
    filer_name: str
    names: dict                      # company id -> display name (all listed companies)
    aliases: dict                    # company id -> set of strings seen in the text
    found: dict                      # paragraph id -> {company id: name} (direct mentions)
    mentions: list = field(default_factory=list)
    unmapped: int = 0

    def cid(self, company_id: str) -> str:
        return company_id if company_id == "F0" else f"C{company_id}"


def load_scope(pdf: str, mentions_path: str, filer_override: str = "") -> Scope:
    paras = c.load_paragraphs(pdf)
    mentions = d.load_mentions(mentions_path, Path(pdf).name)
    if not mentions:
        raise SystemExit(f"no mentions for {Path(pdf).name} in {mentions_path}")
    filer_id, filer_name = d.pick_filer(mentions, filer_override)
    found, unmapped = d.locate(mentions, paras)
    names = {m["company_id"]: m["name"] for m in mentions}
    names.setdefault(filer_id, filer_name)
    aliases: dict = {}
    for m in mentions:
        aliases.setdefault(m["company_id"], set()).update({m["name"] or "", m["text"] or ""})
    return Scope(pdf, paras, {p.pid: p for p in paras}, filer_id, filer_name, names, aliases, found, mentions, unmapped)


def listed_near(scope: Scope, pid: int, window: int) -> dict:
    """Listed companies mentioned directly in paragraphs pid-window .. pid+window ({id: name})."""
    out: dict = {}
    for q in range(pid - window, pid + window + 1):
        out.update(scope.found.get(q, {}))
    return out


def context_lines(scope: Scope, pid: int, before: int = 2, after: int = 1, cut: int = 350, tail: bool = False) -> tuple[str, str]:
    """(text of previous paragraphs, text of next paragraphs) as [ctx] lines, for resolving pronouns. tail=True shows the
    END of each previous paragraph (the words closest to the pronoun) instead of its start."""
    prev = [f"[ctx] {(scope.by_id[q].text[-cut:] if tail else scope.by_id[q].text[:cut])}" for q in range(pid - before, pid) if q in scope.by_id]
    nxt = [f"[ctx] {scope.by_id[q].text[:cut]}" for q in range(pid + 1, pid + after + 1) if q in scope.by_id]
    return "\n".join(prev), "\n".join(nxt)
