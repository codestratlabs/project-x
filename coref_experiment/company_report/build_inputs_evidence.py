#!/usr/bin/env python3
"""The three input tables that used to come from the model's facts cache, rebuilt from the rule-based evidence files.
Same file names and columns, so the models read them unchanged."""
from __future__ import annotations

import collections
import csv
import re
from pathlib import Path

from build_inputs import write

csv.field_size_limit(10**9)


def _hold(ev: Path) -> list:
    return list(csv.DictReader(open(ev / "holdings_from_evidence.csv", encoding="utf-8")))


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def series_prices(ev: Path, out: Path) -> dict:
    """Cost per share by preferred series, the most common value across fund holdings tables (evidence rows A or B)."""
    prices = collections.defaultdict(collections.Counter)
    for h in _hold(ev):
        sh, c = _f(h["shares"]), _f(h["cost_usd"])
        m = re.fullmatch(r"Series ([A-Z])", h["security"])
        if h["position_type"] == "equity" and m and sh and c and sh > 100:
            prices[m[1]][round(c / sh, 2)] += 1
    body = [[k, *v.most_common(1)[0], sum(v.values())] for k, v in sorted(prices.items())]
    return {"series_prices.csv": write(out, "series_prices.csv", ["series", "cost_per_share", "rows_at_this_value", "rows"], body)}


def dated_lots(ev: Path, cfg: dict, out: Path) -> dict:
    """Purchases in fund holdings tables: shares, cost and the printed acquisition date. Per-share cost is split-adjusted and must
    sit within 3x of the funds' mark of that time."""
    marks = {r["month"]: float(r["median_price"]) for r in csv.DictReader(open(out / "data" / "nport_price_monthly.csv", encoding="utf-8"))}
    day, ratio = cfg.get("split_date", "0"), cfg.get("split_ratio", 1)
    near = lambda m: marks[min(marks, key=lambda x: abs(int(x[:4]) * 12 + int(x[5:]) - int(m[:4]) * 12 - int(m[5:7])))]   # noqa: E731
    seen: dict = {}
    for h in _hold(ev):
        d, sh, c = h["acquisition_date"], _f(h["shares"]), _f(h["cost_usd"])
        if h["position_type"] != "equity" or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) or not sh or not c or sh < 100:
            continue
        adj = c / sh / (ratio if (h["as_of_date"] or d) < day else 1)
        if not 0.75 * near(d) <= adj <= 1.35 * near(d):
            continue
        s = re.fullmatch(r"Series ([A-Z])", h["security"])
        k = (d, s[1] if s else "other", round(adj, 2))
        e = seen.setdefault(k, {"n": 0, "holder": h["holder"], "shares": sh, "cost": c, "ids": []})
        e["n"] += 1
        e["ids"].append(h["evidence_id"])
    body = sorted([k[0], k[1], k[2], e["shares"], e["cost"], e["holder"], e["n"], " ".join(e["ids"][:20])] for k, e in seen.items())
    return {"dated_lots.csv": write(out, "dated_lots.csv", ["acquired", "series", "cost_per_share_split_adj", "shares", "cost_usd", "holder", "rows", "evidence_ids"], body)}


def acquisitions(ev: Path, cfg: dict, out: Path) -> dict:
    """Sentences saying the company acquired another company, word for word from the filings."""
    key, seen, body = cfg["company"]["name_regex"], set(), []
    month = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    for r in csv.DictReader(open(ev / "prose_sentences.csv", encoding="utf-8")):
        t = r["sentence"]
        if re.search(r"(^|\b)" + key + r"\b[^.]{0,40}(agreed to acquire|acquired|to acquire|acquisition of)", t, re.I):
            k = re.sub(r"\W+", " ", t.lower())[:60]
            if k not in seen:
                seen.add(k)
                d = re.search(month + r"\s+(?:\d{1,2},\s+)?\d{4}", t)
                body.append([d[0] if d else "", t[:400], t[:400], f"{r['form']} {r['file_date']} {r['accession']}"])
    return {"acquisitions.csv": write(out, "acquisitions.csv", ["as_of", "statement", "quote", "filing"], sorted(body))}
