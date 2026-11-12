#!/usr/bin/env python3
"""Read every number by rule, with no model: iXBRL tags, then HTML tables by their printed column headers, then fixed text layouts.
One row per source cell group. Every row keeps the filing, table, row, printed header, printed unit and raw cells. A row that cannot be tied to a column and a unit goes to review, never into evidence.

    python evidence_tables.py databricks --folders databricks databricks_ncsr databricks_delta
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import html
import json
import multiprocessing as mp
import re
from pathlib import Path

csv.field_size_limit(10**8)
HERE = Path(__file__).parent
MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
COL = [("text", r"\btype\b"), ("date", r"\bdate\b|\bacquired\b"), ("pct", r"%|percent"), ("per_share", r"per\s+share|per\s+unit"), ("unrealized", r"unrealized|appreciation|depreciation"),
       ("foot", r"footnote"), ("qty", r"units?\s*/\s*commitment"), ("funded", r"(?<!un)funded\s+commitment"), ("commitment", r"unfunded|commitment|undrawn"), ("value", r"\b(fair|market)?\s*value\b"), ("cost", r"\bcost\b"), ("qty", r"\bshares\b|\bprincipal\b|\bpar\b|\bunits\b|\bquantity\b|\bface\b|\bnumber of\b"), ("date", r"\bdate\b|\bacquired\b"), ("pct", r"%|percent")]
HEAD_UNIT = re.compile(r"\(?\$?\s*000[’'`]?s?\)?|thousands|millions|\$\s?m\b", re.I)
PAGE_UNIT = re.compile(r"(?:\bin\s|\$\s|expressed in\s|amounts?\s|value\s|cost\s)[^.<]{0,40}?(thousands|millions|\$\s?000[’'`]?s?)\b", re.I)
NUMBER = re.compile(r"^[-−–]?\s*\(?\$?\s*\(?\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*\)?\s*[*†‡#]*\s*(?:\([a-z0-9]{1,3}\)\s*,?\s*)*[a-z]{0,1}$")


def text(x: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", x)).replace("\xa0", " ").replace("​", " ").replace("‌", "")).strip()


def grid(tr: str) -> list:
    """Cells of a row with their column position, colspans expanded."""
    res, col = [], 0
    for attrs, body in re.findall(r"<t[dh]\b([^>]*)>(.*?)</t[dh]>", tr, re.S | re.I):
        m = re.search(r"colspan\s*=\s*[\"']?(\d+)", attrs, re.I)
        span = int(m[1]) if m else 1
        t = text(body)
        if t:
            res.append((col, col + span - 1, t))
        col += span
    return res


def header_of(rows: list) -> list:
    """The printed header cells of a table, or nothing. A header row names a value or a cost column and holds no large number."""
    for tr in rows[:30]:
        g = grid(tr)
        joined = " ".join(c[2] for c in g)
        if g and len(joined) < 240 and re.search(r"\bvalue\b|\bcost\b", joined, re.I) and not re.search(r"\d,\d{3}", joined):
            return g
    return []


def col_of(label: str):
    for name, rx in COL:
        if re.search(rx, label, re.I):
            return name
    return None


def unit_of(label: str):
    if re.search(r"million|\$\s?m\b", label, re.I):
        return 1e6, label
    if re.search(r"\b000|thousand", label, re.I):
        return 1e3, label
    return None, ""


def page_unit(t: str, pos: int):
    """The nearest unit statement above the table, back to the start of its schedule."""
    z = t[max(0, pos - 60000):pos]
    h = max((m.start() for m in re.finditer(r"schedules? of investments|portfolio of investments|statements? of investments", z, re.I)), default=0)
    sl = text(z[max(0, h - 1500):])
    hits = list(PAGE_UNIT.finditer(sl))
    if not hits:
        return None, ""
    m = hits[-1]
    return (1e6 if "million" in m[1].lower() else 1e3), sl[max(0, m.start() - 20):m.end() + 10]


DATE = re.compile(MONTH + r"\s+\d{1,2},\s+\d{4}|\b\d{1,2}/\d{1,2}/\d{4}\b", re.I)
SCHED = re.compile(r"(?:schedules?|statements?|portfolio|summary)\s+of\s+investments|investment\s+portfolio|portfolio\s+holdings|"
                   r"restricted\s+securities|unfunded\s+(?:loan\s+)?commitments", re.I)


def anchors(t: str) -> list:
    """Dated headings of the holdings tables in a filing: (position, date). A row takes the nearest one above it, so a 10-Q
    that prints this quarter's schedule and last year-end's schedule gives each row its own date. Only headings count: the
    date must sit right after (or right before) the words, and a sentence that refers to the schedule ("see the Statement
    of Investments") is not a heading."""
    out = []
    for m in SCHED.finditer(t):
        if re.match(r"restricted|unfunded", m[0], re.I):      # notes inside a schedule: their dates are purchase or expiry dates
            continue
        before, after = text(t[max(0, m.start() - 700):m.start()]), text(t[m.start():m.start() + 1200])
        if re.search(r"\b(see|in|the|of|our|and|to|from|with|per)\s*$", before[-12:], re.I):
            continue
        a = DATE.search(after[:170])
        head = (before[-160:] + " " + after[:170]).strip()
        if a and not re.search(r"\.\s", after[:a.start()]):
            out.append((m.start(), a[0], head))
            continue
        b = list(DATE.finditer(before[-110:]))
        seg = before[-110:][b[-1].end():] if b else ""
        around = before[-110:][max(0, b[-1].start() - 40):] if b else ""
        if b and len(seg.split()) <= 8 and not re.search(r"[a-z]\.\s", seg) and not re.search(r"\b(prior|until|since|was|were|is|are|by|for|from|after|before|through)\b", around, re.I):
            out.append((m.start(), b[-1][0], head))
    return out


def period(t: str, form: str) -> str:
    """The period a fund shareholder report covers ("Date of reporting period: June 30, 2025"). Used only for N-CSR and
    N-CSRS, which hold one schedule per fund at that date; other forms can hold schedules at two dates."""
    if not form.upper().startswith("N-CSR"):
        return ""
    tags = set(re.findall(r'name="dei:DocumentPeriodEndDate"[^>]*>\s*(?:<[^>]+>\s*)*([^<]{6,30}?)\s*<', t[:3000000]))
    tags = {x.strip() for x in tags if DATE.fullmatch(x.strip()) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", x.strip())}
    if len(tags) == 1:
        return tags.pop()                                     # the filing's own tag for the period
    z = text(t[:600000])
    m = re.search(r"date of reporting period\W{0,20}(.{0,160})", z, re.I)
    if not m:
        return ""
    import datetime as dt
    seg = re.split(r"\bItem\s+1\b", m[1], flags=re.I)[0]
    ds = {}
    for d in DATE.finditer(seg):
        x = " ".join(d[0].split()).title()
        for fmt in ("%B %d, %Y", "%m/%d/%Y"):
            try:
                ds.setdefault(dt.datetime.strptime(x, fmt).date(), d[0])
            except ValueError:
                pass
    return next(iter(ds.values())) if len(ds) == 1 else ""      # one date, however many ways it is written


def as_of(anch: list, per: str, pos: int) -> tuple:
    """(date, where the date came from, the heading text of the table the row sits in)."""
    best = [(d, h) for p, d, h in anch if p < pos]
    if best:
        return best[-1][0], "heading of the holdings table", best[-1][1]
    if per:
        return per, "reporting period on the cover of the N-CSR", ""
    return "", "no date found", ""


def from_ixbrl(t: str, key: str) -> list:
    ctx = {}
    for cid, b in re.findall(r'<xbrli:context id="([^"]+)">(.*?)</xbrli:context>', t, re.S):
        m = re.search(r"InvestmentIdentifierAxis\.domain>(.*?)</", b, re.S)
        if m and re.search(key, m[1], re.I):
            inst = re.search(r"<xbrli:instant>(.*?)<", b)
            if inst:
                ctx[cid] = (html.unescape(m[1]), inst[1])
    rows: dict = {}
    for attrs, val in re.findall(r"<ix:nonFraction([^>]*)>(.*?)</ix:nonFraction>", t, re.S) if ctx else []:
        c = re.search(r'contextRef="([^"]+)"', attrs)
        if not c or c[1] not in ctx:
            continue
        nm = re.search(r'name="([^"]+)"', attrs)[1].split(":")[-1]
        sc = re.search(r'scale="(-?\d+)"', attrs)
        raw = re.sub(r"<[^>]+>", "", val).replace(",", "").strip()
        sign = -1 if re.search(r'sign="-"', attrs) else 1                       # iXBRL prints the absolute value; the sign is an attribute
        if re.fullmatch(r"\d*\.?\d+", raw):
            rows.setdefault(c[1], {"name": ctx[c[1]][0], "date": ctx[c[1]][1]})[nm] = sign * float(raw) * 10 ** int(sc[1] if sc else 0)
    out = []
    for cid, r in rows.items():
        q, cs, v = r.get("InvestmentOwnedBalancePrincipalAmount") or r.get("InvestmentOwnedBalanceShares"), r.get("InvestmentOwnedAtCost"), r.get("InvestmentOwnedAtFairValue")
        if q or cs or v:
            out.append(dict(method="iXBRL tag", name=r["name"][:120], qty=q, cost=cs, value=v, date=r["date"], header="iXBRL concepts", unit="scale attribute of the tag", unit_factor=1, raw=json.dumps({k: v_ for k, v_ in r.items() if k not in ("name", "date")})[:300], table=cid, row="", problem=""))
    return out


def from_table(t: str, m, key: str, last: list):
    a = t.rfind("<table", 0, m.start())
    z = t.find("</table>", m.end())
    if a < 0 or z < 0 or t.rfind("</table>", a, m.start()) >= 0 or z - a > 2000000:
        return None
    s = t[a:z]
    trs = re.findall(r"<tr\b[^>]*>(.*?)</tr>", s, re.S | re.I)
    row = next((i for i, tr in enumerate(trs) if key_in(tr, key) and t[a:m.start()].count("<tr") - 1 == i or False), None)
    if row is None:
        row = sum(1 for _ in re.finditer(r"<tr\b", t[a:m.start()], re.I)) - 1
    if row < 0 or row >= len(trs):
        return None
    g = grid(trs[row])
    if not any(re.search(key, c[2], re.I) for c in g):
        return None
    if len(g) == 1 and row + 1 < len(trs):     # the issuer on its own row, its instrument and numbers on the next
        nx = grid(trs[row + 1])
        if any(NUMBER.match(c[2]) for c in nx):
            g = g + nx
    if len(g) == 2 and re.fullmatch(r"\d{1,2}\.\d{1,2}%?", g[1][2]):
        return dict(problem="percent summary, not a position", name=g[0][2][:120], raw=" | ".join(c[2] for c in g)[:300], table=a, row=row, header="", unit="", unit_factor=None, method="table", qty=None, cost=None, value=None, date="")
    h, src = header_of(trs[:row]), "same table"
    if not h and last and last[0]:
        h, src = last[0], "earlier table"
    if h and src == "same table":
        last[:] = [h]
    if not h and not any(NUMBER.match(c[2]) and float(NUMBER.match(c[2])[1].replace(",", "")) >= 100 for c in g):
        return dict(problem="not a position: no amount in the row", name=g[0][2][:120], raw=" | ".join(c[2] for c in g)[:300], table=a, row=row, header="", unit="", unit_factor=None, method="table", qty=None, cost=None, value=None, date="")
    if not h:
        return dict(problem="no column header found", name=g[0][2][:120], raw=" | ".join(c[2] for c in g)[:300], table=a, row=row, header="", unit="", unit_factor=None, method="table", qty=None, cost=None, value=None, date="")
    cols = [(c[0], c[1], col_of(c[2]), unit_of(c[2]), c[2]) for c in h]
    r = dict(method="table", name="", qty=None, cost=None, value=None, commitment=None, date="", table=a, row=row, header=" | ".join(c[2] for c in h)[:200] + f" [{src}]", raw=" | ".join(c[2] for c in g)[:300], problem="")
    r["name"] = next((c[2] for c in g if re.search(key, c[2], re.I)), "")[:140]
    units, pu = {}, page_unit(t, a)
    for ci, c in enumerate(g):
        if re.search(key, c[2], re.I):
            continue
        v = NUMBER.match(c[2])
        nxt = g[ci + 1][2].strip() if ci + 1 < len(g) else ""
        neg = bool(v) and (bool(re.match(r"^\s*(?:[-−–]|\$?\s*\()", c[2])) or (nxt == ")" and "(" in c[2]))   # (44) or -44 is negative
        d = re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}(?:\s*-\s*\d{1,2}/\d{1,2}/\d{2,4})?", c[2])
        target = min(cols, key=lambda k: 0 if k[0] <= c[0] <= k[1] or c[0] <= k[1] <= c[1] else min(abs(c[0] - k[0]), abs(c[1] - k[1])))
        name = target[2]
        if d:
            r["date"] = c[2]
        elif v and name in ("qty", "cost", "value", "commitment") and r[name] is None:
            r[name] = float(v[1].replace(",", "")) * (-1 if neg else 1)
            units[name] = target[3]
    pos_read = {k: r[k] for k in ("qty", "cost", "value", "commitment")}       # reading 1: by column position
    # Amounts sit at the end of a schedule row in the order of the last header columns (par, cost, fair value, % of net
    # assets). When the row ends with as many amount cells as the header ends with amount columns, pair them from the right:
    # this survives header and data cells that are split or spanned differently ($ signs, brackets, empty spacer cells).
    AMT = ("qty", "cost", "value", "commitment", "pct", "per_share", "unrealized", "funded")
    hk = []
    kstart = next((c[0] for c in g if re.search(key, c[2], re.I)), 0)
    for c in sorted([k for k in cols if k[2] != "foot" and k[0] >= kstart], key=lambda k: k[0]):   # columns after the name
        hk = hk + [c] if c[2] in AMT else []
    toks, after_key = [], False
    for ci, c in enumerate(g):
        x = c[2].strip()
        if re.search(key, x, re.I):
            after_key, toks = True, []
            continue
        if not after_key or x in ("", "$", ")", "%", "​") or re.fullmatch(r"[*†‡#]*\s*(?:\d{1,2}|[a-z])(?:\s*,\s*[*†‡#]*(?:\d{1,2}|[a-z]))+[*†‡#]*|[*†‡#]+(?:,\d{1,2})*|\(?[a-z]\)?(?:\s*\([a-z0-9]\))*|(?:\([a-z0-9]{1,3}\)\s*,?\s*){2,}|\([a-z]{1,3}\)", x):
            continue
        if re.fullmatch(r"\(\d{1,3}\)", x):                # (83) is a negative amount or a footnote: decided below
            toks.append(("maybe", -float(x[1:-1])))
            continue
        nxt = g[ci + 1][2].strip() if ci + 1 < len(g) else ""
        if re.fullmatch(r"[—–-]+|N/?A", x, re.I):
            toks.append(("dash", None))
        elif re.fullmatch(r"\(?\s*[\d.]+\s*\)?\s*%", x) or (NUMBER.match(x) and nxt == "%"):
            toks.append(("pct", None))
        elif NUMBER.match(x) and not re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", x):
            v = NUMBER.match(x)
            neg = bool(re.match(r"^\s*(?:[-−–]|\$?\s*\()", x)) or (nxt == ")" and "(" in x)
            toks.append(("num", float(v[1].replace(",", "")) * (-1 if neg else 1)))
        else:
            toks = []                                  # text or a date ends the run: amounts come after it
    if any(k_ == "maybe" for k_, _ in toks):
        as_num = [("num", v) if k_ == "maybe" else (k_, v) for k_, v in toks]
        as_foot = [(k_, v) for k_, v in toks if k_ != "maybe"]
        fit = lambda tt: len(tt) == len(hk) or (len(tt) == len(hk) - 1 and hk and hk[-1][2] in ("pct", "per_share", "unrealized"))   # noqa: E731
        if any(c[2] == "commitment" for c in hk):        # commitment tables print negative fair values, not footnotes
            toks = as_num if fit(as_num) else toks
        elif fit(as_num) and len(as_num) == len(hk):     # the bracket fills the last column exactly
            toks = as_num
        elif fit(as_foot) and not fit(as_num):
            toks = as_foot
        if any(k_ == "maybe" for k_, _ in toks):
            r["problem"] = "a bracketed number could be a footnote or a negative amount"
    if len(hk) > len(toks) >= 2 and len(hk) >= 2 and hk[0][2] == hk[1][2] == "qty":
        hk = hk[1:]                                    # "Shares/Units" and "Principal": a row fills one of them
    while hk and len(toks) < len(hk) and hk[-1][2] in ("pct", "per_share", "unrealized"):
        hk = hk[:-1]                                   # a row may leave out the trailing percent column
    if not r["problem"] and len(hk) >= 1 and len(toks) > len(hk) and any(c[2] in ("qty", "cost", "value", "commitment") for c in hk):
        r["problem"] = f"{len(toks)} amount cells at the end of the row but {len(hk)} amount columns in the header"   # no guessing which is which
    if not r["problem"] and len(hk) >= 1 and len(toks) == len(hk) and any(c[2] in ("qty", "cost", "value", "commitment") for c in hk):
        r["align"] = "right-aligned with the header"
        for c in hk:                                   # the columns paired from the right are set again; others keep their reading
            if c[2] in ("qty", "cost", "value", "commitment"):
                r[c[2]] = None
                units.pop(c[2], None)
        for c, (kind, v) in zip(hk, toks[-len(hk):]):
            if c[2] in ("qty", "cost", "value", "commitment") and kind == "num":
                r[c[2]] = v
                units[c[2]] = c[3]
    # reading 2 (right-aligned) must agree with reading 1 on every column both of them read
    if r.get("align"):
        both = [k for k in ("qty", "cost", "value", "commitment") if r[k] is not None or pos_read[k] is not None]
        r["methods"] = "two readings agree" if all(r[k] == pos_read[k] for k in both) else \
            "two readings differ: " + "; ".join(f"{k} {pos_read[k]} vs {r[k]}" for k in both if r[k] != pos_read[k])
    else:
        r["methods"] = "one reading (by column position)"
    cost = re.search(r"cost\s*(?:[:—–-]\s*)?\$\s*([\d,]+(?:\.\d+)?)", r["name"], re.I)
    if cost and r["cost"] is None:
        r["cost"] = float(cost[1].replace(",", ""))
        units["cost"] = units.get("value") or (None, "")
    f = [u[0] for k, u in units.items() if k != "qty" and u and u[0]]
    if f:
        r["unit_factor"], r["unit"] = f[0], next(u[1] for k, u in units.items() if k != "qty" and u and u[0])
    elif pu[0]:
        r["unit_factor"], r["unit"] = pu
    elif "$" in r["header"] and not re.search(r"\b000|thousand|million", r["header"], re.I):
        r["unit_factor"], r["unit"] = 1, "dollars, $ in the column header"
    else:
        r["unit_factor"], r["unit"] = 1, "no unit printed; dollars"
    q = units.get("qty")
    label = next((k[4] for k in cols if k[2] == "qty"), "")
    loan_row = bool(re.search(r"term\s+loan|delayed\s+draw|revolv|first\s+lien|1st\s+lien|senior\s+secured|\bloan\b|SOFR|\bS\s*\+", r["raw"], re.I))
    money_qty = (bool(re.search(r"principal|\bpar\b|amount|face", label, re.I)) and not re.search(r"shares", label, re.I)) or loan_row   # par is in the table's money unit
    r["qty_factor"] = q[0] if q and q[0] else (r["unit_factor"] if money_qty else 1)
    if r["problem"]:
        return r
    if r["qty"] is None and r["cost"] is None and r["value"] is None and r["commitment"] is None:
        r["problem"] = "no number under a known column" if any(NUMBER.match(c[2]) and float(NUMBER.match(c[2])[1].replace(",", "")) >= 100 for c in g) else "not a position: no amount in the row"
    return r


def key_in(tr, key):
    return bool(re.search(key, tr, re.I))


def from_text(t: str, m, key: str):
    """Layouts without table rows: name first, then the footnotes, then shares and value."""
    x = text(t[m.start():m.start() + 1400])
    r = re.match(r"(.{0,170}?(?:\)|\.{2,}|\bDate:[^,]{0,20}, Cost \$[\d,]+[^0-9]{0,4}))\s*(?:\([\da-z]+\)\s*)*[.\s]*(\d[\d,]*)\s+(\d[\d,]*)\b", x)
    if not r or not re.search(r"series|inc|preferred|common", r[1], re.I):
        return None
    z = text(t[max(0, m.start() - 1500):m.start()])
    if not re.search(r"\.{2,}|Cost \$|\(\d\)|\([a-z]\)", r[0]):
        return None
    f, u = page_unit(t, m.start())
    c = re.search(r"cost[:\s]*\$\s*([\d,]+(?:\.\d+)?)", r[0], re.I)
    return dict(method="text layout", name=r[1][:140], qty=float(r[2].replace(",", "")), cost=float(c[1].replace(",", "")) if c else None, value=float(r[3].replace(",", "")), date="", table="text", row=m.start(), header="shares then value, as laid out", unit=u or "no unit printed; dollars", unit_factor=f or 1, raw=x[:300], problem="", qty_factor=1)


PDF_PROSE: list = []


def from_ncen(t: str, key: str) -> list:
    """Form N-CEN is XML: a principal transaction names the company as the counterparty of the fund's purchases or sales
    over the fund's fiscal year. Read the fields directly."""
    end = re.search(r'reportEndingPeriod="(\d{4}-\d{2}-\d{2})"', t)
    out = []
    for m in re.finditer(r"<principalTransaction>(.*?)</principalTransaction>", t, re.S):
        nm = re.search(r"<principalName>(.*?)</principalName>", m[1], re.S)
        amt = re.search(r"<principalTotalPurchaseSale>([\d.]+)<", m[1])
        if nm and amt and re.search(key, html.unescape(nm[1]), re.I):
            q = t.rfind("<managementInvestmentQuestion", 0, m.start())
            fund = re.search(r"<mgmtInvFundName>(.*?)</mgmtInvFundName>", t[q:m.start()], re.S) if q >= 0 else None
            out.append(dict(method="N-CEN XML", name=f"principal transaction with {html.unescape(nm[1])}", qty=None, cost=None, value=float(amt[1]),
                            date="", table="principalTransactions", row=m.start(), header="principalTotalPurchaseSale", unit="dollars as filed (XML field)",
                            unit_factor=1, raw=re.sub(r"\s+", " ", m[1])[:300], problem="", qty_factor=1, as_of=end[1] if end else "",
                            as_of_source="N-CEN reportEndingPeriod" if end else "no date found", schedule=html.unescape(fund[1]) if fund else ""))
    return out


def from_pdf(p: Path, key: str) -> list:
    """PDF lines naming the company. Numbers are read right to left against the nearest printed header line above; a line that does not fit goes to review."""
    import pypdfium2
    doc = pypdfium2.PdfDocument(str(p))
    out = []
    labels = [("date", r"date"), ("qty", r"shares|principal|par\b|units"), ("cost", r"cost"), ("value", r"value")]
    for i in range(len(doc)):
        pg = doc[i]
        tp = pg.get_textpage()
        txt = tp.get_text_range()
        tp.close()
        pg.close()
        if not re.search(key, txt, re.I):
            continue
        lines = [re.sub(r"\s+", " ", x).strip() for x in txt.splitlines()]
        u = PAGE_UNIT.search(re.sub(r"\s+", " ", txt))
        unit = (1e6 if "million" in u[1].lower() else 1e3) if u else None
        when = re.findall(MONTH + r" \d{1,2}, \d{4}", txt)
        for j, ln in enumerate(lines):
            if not re.search(key, ln, re.I):
                continue
            nums = [float(x.replace(",", "")) for x in re.findall(r"(?<![\d/.])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\d/])", ln)]
            if not nums or len(ln) > 160:                  # prose, not a table line: keep the sentence for facts
                flat = re.sub(r"\s+", " ", " ".join(lines[max(0, j - 3):j + 4]))
                for snt in re.split(r"(?<=[.!?])\s+(?=[A-Z])", flat):
                    if re.search(key, snt, re.I):
                        PDF_PROSE.append({"pos": f"page {i + 1}", "sentence": snt[:1500], "near_heading_date": ""})
                continue
            head = next((h for h in reversed(lines[max(0, j - 60):j]) if re.search(r"\b(value|cost)\b", h, re.I) and not re.search(r"\d,\d{3}", h) and len(h) < 200), "")
            order = sorted((re.search(rx, head, re.I).start(), n) for n, rx in labels[1:] if re.search(rx, head, re.I))
            cols = [n for _, n in order]
            r = dict(method="pdf line", name=ln[:140], qty=None, cost=None, value=None, date="", table=f"page {i + 1}", row=j, header=head[:200], unit="" , unit_factor=unit or 1, raw=ln[:300], problem="", qty_factor=1, as_of=when[0] if when else "", as_of_source="first date printed on the PDF page" if when else "no date found")
            r["unit"] = u[0][:60] if u else "no unit printed; dollars"
            if cols and len(nums) >= len(cols):
                for n, v in zip(cols, nums[-len(cols):]):
                    r[n] = v
            else:
                r["problem"] = "pdf line does not fit the printed header"
            out.append(r)
    doc.close()
    return out


def sentence(t: str, m) -> str:
    """The sentence (or list item / short block) that names the company, cut at sentence ends; for prose facts."""
    a, b = text(t[max(0, m.start() - 1500):m.start()]), text(t[m.start():m.start() + 1500])
    left = re.split(r"(?<=[.!?;•●])\s+(?=[A-Z“\"(•●])", a)[-1]
    right = re.split(r"(?<=[.!?])\s+(?=[A-Z“\"(•●])", b, maxsplit=1)[0]
    return (left + " " + right).strip()[:1500]


def scan(job: tuple) -> dict:
    path, key, cache, form, acc = job
    c = Path(cache) / (hashlib.sha1(f"{acc}/{Path(path).name}".encode()).hexdigest() + ".json")   # by filing, not by folder: works on any machine
    if c.exists():
        res = json.loads(c.read_text(encoding="utf-8"))
        res["path"] = path
        return res
    if not Path(path).exists():
        return {"path": path, "rows": [], "other": 0, "error": "filing not on disk and not in the cache"}
    res = {"path": path, "rows": [], "other": 0, "error": ""}
    try:
        p = Path(path)
        if p.suffix.lower() == ".pdf":
            PDF_PROSE.clear()
            res["rows"] += from_pdf(p, key)
            seen_p = set()
            res["prose"] = [x for x in PDF_PROSE if not (x["sentence"] in seen_p or seen_p.add(x["sentence"]))]
        elif p.suffix.lower() == ".xml":
            res["rows"] += from_ncen(p.read_text(encoding="utf-8", errors="ignore"), key)
        else:
            t = p.read_text(encoding="utf-8", errors="ignore")
            t = re.sub(r"</?(?:table|tr|td|th)\b", lambda x: x[0].lower(), t, flags=re.I)     # older filings use capital tags
            tagged = from_ixbrl(t, key)
            for r in tagged:
                r["as_of"], r["as_of_source"] = r["date"], "iXBRL context date"
            res["rows"] += tagged
            anch, per = anchors(t), period(t, form)
            last, seen, prose_seen = [[]], set(), set()
            for m in re.finditer(key, t, re.I):
                r = from_table(t, m, key, last)
                if r is None:
                    r = from_text(t, m, key)
                if r is None:
                    res["other"] += 1
                    sent = sentence(t, m)
                    if sent and sent not in prose_seen:
                        prose_seen.add(sent)
                        d, src, _ = as_of(anch, per, m.start())
                        res.setdefault("prose", []).append({"pos": m.start(), "sentence": sent, "near_heading_date": d if src.startswith("heading") else ""})
                    continue
                k = (r.get("table"), r.get("row"))
                if k in seen:
                    continue
                seen.add(k)
                r["as_of"], r["as_of_source"], r["schedule"] = as_of(anch, per, m.start())
                res["rows"].append(r)
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"[:160]
    c.write_text(json.dumps(res), encoding="utf-8")
    return res


def limit() -> None:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (1400 * 1024 ** 2, 1400 * 1024 ** 2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("company")
    ap.add_argument("--folders", nargs="+", required=True)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--dumps", default=str(HERE.parents[2] / "dumps"))
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    cfg = json.loads((HERE / "model_config" / f"{a.company}.json").read_text(encoding="utf-8"))
    key, ev = cfg["company"]["name_regex"], HERE / "out" / a.company / "evidence"
    cache = ev / "table_cache"
    cache.mkdir(parents=True, exist_ok=True)
    files = {}
    for f in a.folders:
        for r in csv.DictReader(open(Path(a.dumps) / f"{f}_manifest.csv", encoding="utf-8", newline="")):
            p = Path(a.dumps) / f / r["file"]
            if p.suffix.lower() in (".htm", ".html", ".pdf", ".xml") and (not a.only or re.search(a.only, r["form"])):
                files[str(p)] = r
    jobs = sorted(((p, key, str(cache), files[p]["form"], files[p]["accession"]) for p in files), key=lambda j: Path(j[0]).stat().st_size if Path(j[0]).exists() else 0)
    head = ["accession", "form", "file_date", "filer", "as_of", "as_of_source", "schedule", "method", "name", "qty", "qty_factor", "cost", "value", "commitment", "unit_factor", "date", "header", "unit", "raw", "table", "row", "problem", "align", "methods", "url"]
    n = collections.Counter()
    pf = open(ev / "prose_sentences.csv", "w", newline="", encoding="utf-8")
    pw = csv.DictWriter(pf, ["accession", "form", "file_date", "filer", "url", "pos", "near_heading_date", "sentence"])
    pw.writeheader()
    with mp.Pool(a.workers, initializer=limit, maxtasksperchild=25) as pool, open(ev / "parsed_rows.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, head, extrasaction="ignore")
        w.writeheader()
        for i, s in enumerate(pool.imap_unordered(scan, jobs, chunksize=1)):
            m = files[s["path"]]
            n["other_mentions"] += s["other"]
            n["errors"] += bool(s["error"])
            for r in s["rows"]:
                n[r["method"] + (" problem" if r.get("problem") else "")] += 1
                w.writerow({**r, "accession": m["accession"], "form": m["form"], "file_date": m["file_date"], "filer": m["filer"], "url": m["sec_url"]})
            for x in s.get("prose", []):
                pw.writerow({**x, "accession": m["accession"], "form": m["form"], "file_date": m["file_date"], "filer": m["filer"], "url": m["sec_url"]})
                n["prose sentences"] += 1
            if i % 200 == 0:
                print("scanned", i, "of", len(jobs), flush=True)
    pf.close()
    print(json.dumps(dict(n)))


if __name__ == "__main__":
    main()
