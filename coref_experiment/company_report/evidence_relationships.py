#!/usr/bin/env python3
"""Relationships for the report, checked by rule.

    python evidence_relationships.py <company> "<company name>" --folders databricks databricks_ncsr ...

1. From holdings tables (no model): a fund whose holdings table lists the company holds it (invests_in) or lends to it
   (lends_to). One row per fund, with its evidence ids. These replace the model's invests_in / lends_to rows that came
   from the same filings.
2. From prose (model rows, kept only if the check passes): the model's quote must be in the filing word for word (after
   spacing), must not be cut with "...", and must name both companies. A row that fails goes to review with the reason;
   it is not shown in the report.

Output: out/<co>/evidence/relationships_from_evidence.csv and relationships_review.csv
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import html
import re
from pathlib import Path

csv.field_size_limit(10**9)
ap = argparse.ArgumentParser()
ap.add_argument("company")
ap.add_argument("name")
ap.add_argument("--folders", nargs="+", required=True)
ap.add_argument("--with-model-quotes", action="store_true", help="also keep earlier model rows whose quote passes the check (off: model rows go to review only)")
a = ap.parse_args()
HERE = Path(__file__).parent
ROOT = HERE.parents[2]
ev = HERE / "out" / a.company / "evidence"
short = a.name.split(",")[0].split()[0]
NAME = re.compile(rf"\b{re.escape(short)}\b", re.I)
STOP = {"inc", "corp", "corporation", "llc", "lp", "ltd", "limited", "plc", "co", "the", "company", "fund", "funds", "trust", "group",
        "holdings", "capital", "partners", "series", "and", "of", "&"}


def norm(s: str) -> str:
    s = html.unescape(s).replace("’", "'").replace("“", '"').replace("”", '"').replace("—", "-").replace("–", "-")
    return re.sub(r"\s+", " ", re.sub(r"[^\w$%.,'\"-]+", " ", s)).strip().lower()


def words_of(company: str) -> list:
    """Distinctive words of a company name ("MICROSTRATEGY INC" -> ["microstrategy"])."""
    w = [x for x in re.findall(r"[a-z0-9]+", re.sub(r"\(cik \d+\)", "", company.lower())) if x not in STOP and len(x) > 2]
    return w[:2]


hold = list(csv.DictReader(open(ev / "holdings_from_evidence.csv", encoding="utf-8")))
out, review = [], []
by = collections.defaultdict(list)
for h in hold:
    rel = "lends_to" if h["position_type"] in ("funded_loan", "unfunded_commitment") else "invests_in"
    by[(h["holder"], rel)].append(h)
for (holder, rel), hs in by.items():
    hs.sort(key=lambda h: h["as_of_date"])
    out.append(dict(rel_id="R" + hashlib.sha1(f"{holder}|{rel}".encode()).hexdigest()[:10], from_company=holder, relation=rel, to_company=a.name,
                    basis="holdings table (rule)", first_date=hs[0]["as_of_date"], last_date=hs[-1]["as_of_date"], rows=len(hs),
                    evidence_ids=" ".join(h["evidence_id"] for h in hs)[:2000], quote=hs[-1]["raw"][:200], form=hs[-1]["form"],
                    filer=hs[-1]["filer"], filing_date=hs[-1]["filing_date"], sec_url=hs[-1]["sec_url"], check="read from the table", why=""))
schedule_filings = {h["sec_url"] for h in hold} | {h["filer"] for h in hold}

# local file of each filing, to check the model's quotes against the text
local = {}
for f in a.folders:
    man = ROOT / "dumps" / f"{f}_manifest.csv"
    if man.exists():
        for r in csv.DictReader(open(man, encoding="utf-8")):
            local[r["sec_url"]] = ROOT / "dumps" / f / r["file"]
text_cache: dict = {}


def filing_text(url: str) -> str:
    if url not in text_cache:
        p = local.get(url)
        if not p or not p.exists():
            text_cache[url] = ""
        elif p.suffix.lower() == ".pdf":
            import pypdfium2
            d = pypdfium2.PdfDocument(str(p))
            text_cache[url] = norm(" ".join(d[i].get_textpage().get_text_range() for i in range(len(d))))
            d.close()
        else:
            text_cache[url] = norm(re.sub(r"<[^>]+>", " ", p.read_text(encoding="utf-8", errors="ignore")))
        if len(text_cache) > 40:                      # keep memory small
            text_cache.pop(next(iter(text_cache)))
    return text_cache[url]


rel_path = HERE / "out" / a.company / "relationships_all.csv"      # the model's rows from an earlier LLM run of build_report, if any
rows = [r for r in csv.DictReader(open(rel_path, encoding="utf-8"))
        if NAME.search(r["from_company"] + " " + r["to_company"])] if rel_path.exists() else []
rows.sort(key=lambda r: r["sec_url"])
for r in rows:
    if r["relation"] in ("invests_in", "lends_to") and (r["sec_url"] in schedule_filings or r["filer"] in schedule_filings):
        continue                                       # replaced by the rows read from the holdings table
    other = r["to_company"] if NAME.search(r["from_company"]) else r["from_company"]
    q = r["evidence_quote"]
    why = []
    if "..." in q or "…" in q:
        why.append("quote is cut with '...'")
    if not NAME.search(q):
        why.append(f"quote does not name {short}")
    ow = words_of(other)
    if ow and not any(re.search(rf"\b{re.escape(w)}", q, re.I) for w in ow) and not re.search(r"\b(we|our|us|the company)\b", q, re.I):
        why.append(f"quote does not name {other}")
    if not why:
        t = filing_text(r["sec_url"])
        if not t:
            why.append("filing not on disk to check the quote")
        elif norm(q) not in t:
            why.append("quote not found word for word in the filing")
    row = dict(rel_id="R" + hashlib.sha1(f"{r['sec_url']}|{r['from_company']}|{r['relation']}|{r['to_company']}|{q}".encode()).hexdigest()[:10],
               from_company=r["from_company"], relation=r["relation"], to_company=r["to_company"], basis="prose (model), quote checked",
               first_date=r["filing_date"], last_date=r["filing_date"], rows=1, evidence_ids="", quote=q, form=r["form"], filer=r["filer"],
               filing_date=r["filing_date"], sec_url=r["sec_url"], check="direction from the model: " + r["direction_check"], why="; ".join(why))
    if not why and not a.with_model_quotes:
        why.append("from the model; not used (rule-only run)")
        row["why"] = "; ".join(why)
    (review if why else out).append(row)
head = ["rel_id", "from_company", "relation", "to_company", "basis", "first_date", "last_date", "rows", "evidence_ids", "quote", "form",
        "filer", "filing_date", "sec_url", "check", "why"]
for name, rr in (("relationships_from_evidence.csv", out), ("relationships_review.csv", review)):
    with open(ev / name, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, head)
        w.writeheader()
        w.writerows(rr)
print(len(out), "relationships kept", dict(collections.Counter(r["basis"] for r in out)), "|", len(review), "to review",
      dict(collections.Counter(w for r in review for w in r["why"].split("; "))))
