#!/usr/bin/env python3
"""Facts about the company from prose sentences, by rule; no model. A number is kept only with the scale word printed next
to it ("$62 billion", "$1.2 million"), so the scale is never guessed.

    python evidence_facts.py <company> "<company name>"

Input:  out/<co>/evidence/prose_sentences.csv (every sentence of every filing that names the company, from evidence_tables.py)
Output: out/<co>/evidence/facts_from_evidence.csv   one row per number: metric, value, unit as printed, date, sentence, filing
        out/<co>/evidence/facts_review.csv          numbers whose metric or subject is not clear by rule (a person or the
                                                    final model step decides; they are not used until then)

Metric:   from words next to the number in the same sentence (valuation, revenue run-rate, revenue, funding round, debt,
          employees, customers). None or more than one -> review.
Subject:  the company must be the nearest company name before the number, or the sentence must have no other company name.
Date:     a date in the same sentence ("as of June 30, 2025", "in March 2025"); without one the fact keeps the filing date
          and says so (date_source), and the report does not place it in a year by itself.
"""
from __future__ import annotations

import collections
import csv
import hashlib
import re
import sys
from pathlib import Path

csv.field_size_limit(10**9)
co, target = sys.argv[1], sys.argv[2]
HERE = Path(__file__).parent
ev = HERE / "out" / co / "evidence"
short = re.escape(target.split(",")[0].split()[0])
NAME = re.compile(rf"\b{short}\b", re.I)
SCALE = {"trillion": 1e12, "billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mm": 1e6, "mn": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}
MONEY = re.compile(r"(?:US)?\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(trillion|billion|million|thousand|bn|mm|mn|[bmk])?\b", re.I)
COUNT = re.compile(r"\b(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(thousand|million)?\s+(employees|customers|users|organizations|companies)\b", re.I)
MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
DATE = re.compile(rf"{MONTH}\s+\d{{1,2}},\s+\d{{4}}|{MONTH}\s+\d{{4}}|\b(?:Q[1-4]|fiscal(?:\s+year)?)\s+(?:FY)?\d{{4}}\b|(?<=\bin\s)(?:19|20)\d\d\b|(?<=\bas of\s)(?:19|20)\d\d\b|(?<=\bduring\s)(?:19|20)\d\d\b", re.I)
METRIC = [("investor_position_value", rf"(?:investment|interest|stake|position|holding)s?\s+in\s+{short}[^.]{{0,40}}(?:valued|carried|fair\s+value)|{short}\s+(?:investment|position)\s+(?:was\s+)?valued"),
          ("valuation", r"valuation|valued\s+at|post-money|pre-money|implied\s+value|worth"),
          ("revenue_run_rate", r"run[-\s]rate|annuali[sz]ed\s+revenue|\bARR\b|annual\s+recurring"),
          ("revenue", r"\brevenues?\b|\bsales\b"),
          ("funding_round", r"\braised\b|\braising\b|funding|equity\s+financing|financing\s+round|series\s+[a-z]\b|round\s+of|investment\s+round|tender\s+offer"),
          ("debt", r"credit\s+facility|term\s+loan|\bdebt\b|borrow|revolving|\bnotes\b"),
          ("acquisition", r"\bacquir|\bacquisition|\bpurchase[ds]?\s+(?:of|for)")]
OTHER_CO = re.compile(r"\b([A-Z][\w&.\-]+(?:\s+[A-Z][\w&.\-]+){0,3}),?\s+(?:Inc|Corp|Corporation|LLC|L\.P\.|Ltd|Limited|plc|Co)\b\.?")


def date_in(s: str, pos: int) -> str:
    """A date in the same sentence as the number (footnote numbers like ". 11 The" end a sentence); one after the number
    first ("in its quarter ended January 2026"), else the nearest one before it."""
    st = 0
    for m in re.finditer(r"(?<=[.!?])\s+(?=(?:\d{1,2}\s+)?[A-Z])", s):
        if m.end() <= pos:
            st = m.end()
        else:
            break
    en = next((m.start() for m in re.finditer(r"(?<=[.!?])\s+(?=(?:\d{1,2}\s+)?[A-Z])", s) if m.start() > pos), len(s))
    ds = [d for d in DATE.finditer(s) if st <= d.start() < en]
    cs = max([st] + [m.end() for m in re.finditer(r",\s", s[:pos]) if m.end() > st])           # the clause of the number
    ce = min([en] + [m.start() for m in re.finditer(r",\s", s) if m.start() > pos])
    same = [d for d in ds if cs <= d.start() < ce]
    before = [d for d in same if d.start() < pos]
    after = [d for d in same if d.start() > pos]
    if before:
        return before[-1][0]
    if after:
        return after[0][0]
    return ""


CAPWORD = re.compile(r"\b[A-Z][a-zA-Z&.'’-]+")
NOT_NAME = {"Series", "The", "In", "On", "As", "At", "Its", "It", "This", "That", "Inc", "Inc.", "LLC", "Company", "U.S.", "US", "AI", "Our", "We",
            "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December",
            "Q1", "Q2", "Q3", "Q4", "ARR", "Fund", "Funds", "Million", "Billion", "December", "Preferred", "Common", "Class"}


PRON = re.compile(r"\b(?:the company|the firm|its|it)\b", re.I)
CLAUSE = re.compile(r"[;:]|,\s*(?:with|while|whereas|including|and its|but)\b|\bwith\s+(?:its\s+)?(?:AI|[a-z]+\s+products?)\b")


def clean_gap(gap: str) -> bool:
    names = [w for w in CAPWORD.findall(gap) if w.rstrip(".,") not in NOT_NAME and not NAME.search(w)]
    return not names and not CLAUSE.search(gap)


def subject_ok(s: str, a: int, b: int) -> bool:
    """The company is the subject of the number:
    - its name is at most 30 words before the number, or at most 6 words after it joined by "of/for/in/at", with no
      other proper name and no new clause ("..., with AI products") in between; or
    - "the company" / "it" / "its" stands between, and the nearest name before that word is the company, with no other
      proper name in between (one sentence later at most)."""
    for m in NAME.finditer(s):
        if m.end() <= a:
            gap = s[m.end():a]
        elif m.start() >= b and re.match(r"\s*(?:\w+\s+){0,3}(?:of|for|in|at)\s+$", s[b:m.start()]):
            gap = s[b:m.start()]
        else:
            continue
        if len(gap.split()) <= 30 and clean_gap(gap):
            return True
    prons = [p for p in PRON.finditer(s[:a]) if len(s[p.end():a].split()) <= 15]
    for p in prons[-1:]:
        if not clean_gap(s[p.end():a]):
            continue
        ante = [m for m in NAME.finditer(s[:p.start()])]
        if ante and len(s[ante[-1].end():p.start()].split()) <= 45:
            names = [w for w in CAPWORD.findall(s[ante[-1].end():p.start()]) if w.rstrip(".,") not in NOT_NAME and not NAME.search(w)]
            if not names:
                return True
    return False


def metric_of(s: str, a: int, b: int) -> list:
    """The metric whose word is nearest the number (within 90 characters before or 60 after). A position value of an
    investor is never the company's valuation; a run-rate is not plain revenue. Two metrics equally near -> unclear."""
    if re.search(METRIC[0][1], s[max(0, a - 160):b + 10], re.I):
        return ["investor_position_value"]
    for m, rx in METRIC[1:]:                       # "$5 billion of equity financing": the words right after the number name it
        if re.match(rf"\s*(?:of|in)\s+(?:[\w-]+\s+){{0,2}}?(?:{rx})", s[b:b + 60], re.I):
            return [m]
    best = {}
    for m, rx in METRIC[1:]:
        for x in re.finditer(rx, s[max(0, a - 90):b + 60], re.I):
            xs, xe = x.start() + max(0, a - 90), x.end() + max(0, a - 90)
            between = s[xe:a] if xe <= a else s[b:xs]
            if re.search(r"[;:]|,\s*(?:with|and|while|including|whereas|but)\b|\(", between):
                continue                                   # the word belongs to another clause
            best[m] = min(best.get(m, 10**6), abs(xs - a))
    if "revenue_run_rate" in best and "revenue" in best:
        best.pop("revenue")
    if not best:
        return []
    ranked = sorted(best.items(), key=lambda x: x[1])
    if len(ranked) > 1 and ranked[1][1] - ranked[0][1] < 15:
        return [m for m, _ in ranked]
    return [ranked[0][0]]


facts, review = [], []
seen = set()
for r in csv.DictReader(open(ev / "prose_sentences.csv", encoding="utf-8")):
    s = re.sub(r'^[^<>]{0,80}?"\s*>\s*', "", r["sentence"])      # a sentence cut inside an HTML tag
    if not NAME.search(s):
        continue
    found = [(m.start(), m.end(), float(m[1].replace(",", "")) * SCALE.get((m[2] or "").lower(), 1), m[0], "USD") for m in MONEY.finditer(s)]
    found += [(m.start(), m.end(), float(m[1].replace(",", "")) * SCALE.get((m[2] or "").lower(), 1), m[0], m[3].lower()) for m in COUNT.finditer(s)]
    for a, b, val, printed, unit in found:
        mets = ["employees" if unit == "employees" else "customers_users"] if unit != "USD" else metric_of(s, a, b)
        d = date_in(s, a)
        row = dict(metric=mets[0] if len(mets) == 1 else "", value=val, unit=unit, printed=printed,
                   date=d, date_source="nearest date in the same sentence" if d else "not stated; filing date shown", filing_date=r["file_date"],
                   form=r["form"], filer=r["filer"], accession=r["accession"], url=r["url"], sentence=s[:900])
        row["fact_id"] = "F" + hashlib.sha1(f"{r['accession']}|{r['pos']}|{a}|{printed}".encode()).hexdigest()[:10]
        k = (row["metric"], round(val), row["date"], s[:200])
        if k in seen:
            continue
        seen.add(k)
        why = []
        if unit == "USD" and val < 1e5 and not re.search(r"per\s+share|a\s+share", s[b:b + 30], re.I):
            why.append("no scale word next to a small dollar figure")
        if len(mets) != 1:
            why.append("metric not clear by rule" if mets else "no metric word next to the number")
        if not subject_ok(s, a, b):
            why.append("number not tied to the company by rule (too far, or another name in between)")
        if re.search(r"per\s+share|a\s+share", s[b:b + 30], re.I):
            row["metric"] = (row["metric"] or "price") + "_per_share"
        (review if why else facts).append({**row, "why": "; ".join(why)})
# Form N-CEN principal transactions: a fund's purchases or sales with the company over its fiscal year (XML fields)
for r in csv.DictReader(open(ev / "evidence_rows.csv", encoding="utf-8")):
    if r["method"] == "N-CEN XML" and r["tier"] == "A XML":
        facts.append(dict(fact_id=r["evidence_id"], metric="fund_transaction", value=r["value_usd"], unit="USD", printed=f"${float(r['value_usd']):,.0f}",
                          date=r["as_of"], date_source="N-CEN reportEndingPeriod (fund fiscal year end)", filing_date=r["file_date"], form=r["form"],
                          filer=r["filer"], accession=r["accession"], url=r["url"], sentence=f"{r['schedule']}: {r['raw']}"[:900], why=""))
head = ["fact_id", "metric", "value", "unit", "printed", "date", "date_source", "filing_date", "form", "filer", "accession", "url", "sentence", "why"]
for name, rows in (("facts_from_evidence.csv", facts), ("facts_review.csv", review)):
    with open(ev / name, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, head)
        w.writeheader()
        w.writerows(rows)
print(len(facts), "facts by rule;", len(review), "to review;", dict(collections.Counter(f["metric"] for f in facts)))
