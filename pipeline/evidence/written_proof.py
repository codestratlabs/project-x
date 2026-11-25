#!/usr/bin/env python3
"""For every holding: each number as written in the filing, the unit and the date as written, and a Match computed from them.

The filing is read again at the row's place (table + row, text position, or iXBRL context). Nothing is taken from the
pipeline's own values except to compare against:
  number cells   the cell text of the row, as printed (brackets, minus, footnote marks kept). A cell matches a pipeline
                 value when (number in the cell) x (scale) = the pipeline value exactly. The minus sign counts.
  iXBRL          the printed text of the tag and of its table cell, plus the tag's sign and scale attributes.
  unit           the words that set the scale ("dollar amounts in thousands", "($000s)"), in the column header or the
                 page heading, as printed; it must give the same scale the pipeline used.
  as of          the words that hold the date: the schedule heading, the "date of reporting period" line of an N-CSR
                 cover or its dei:DocumentPeriodEndDate tag, or the iXBRL context of the tags.
Match is "Yes" only when every pipeline number, the scale and the date are found this way; otherwise "No:" and why.

    python written_proof.py <company>      -> out/<co>/evidence/written_proof.csv (one row per holding, in holdings order)
"""
from __future__ import annotations

import csv
import datetime as dt
import html
import re
import sys
from pathlib import Path

csv.field_size_limit(10**9)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
import paths as px  # noqa: E402
import prose_reader as PR  # noqa: E402

MONTHS = "January February March April May June July August September October November December".split()
FOOT = re.compile(r"(?:\s*(?:\(\s*[a-z0-9]{1,3}\s*\)|[*†‡§#^@]))+\s*$", re.I)
SCHED = re.compile(r"(?:schedules?|statements?|portfolio|summary)\s+of\s+investments|investment\s+portfolio|portfolio\s+holdings|restricted\s+securities|unfunded|investments\s+in\s+securities", re.I)
SCHED_HEAD = re.compile(r"(?:schedules?|statements?|summary)\s+of\s+investments|portfolio\s+of\s+investments|investment\s+portfolio\b", re.I)
DOLLARS = re.compile(r"[^|]{0,30}\(\s*(?:U\.?S\.?\s*)?\$\s*\)", re.I)     # "Value ($)": the column says whole dollars
UNIT = re.compile(r"\(?[^()|.]{0,40}?(?:\bin\s+thousands|\bin\s+millions|\$\s?0{3}[’'`]?s?\b|\(\s*\$?\s*0{3}[’'`]?s?\s*\)|\b0{3}[’'`]?s\s+omitted|\bthousands\s+of\s+(?:u\.s\.\s+)?dollars|\bmillions\s+of\s+(?:u\.s\.\s+)?dollars|\(\s*\$?\s*(?:thousands|millions)\s*\))[^()|]{0,30}\)?", re.I)


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", s)).replace("\xa0", " ").replace("​", " ").replace("‌", "")).strip()


def num(cell: str):
    """The number a printed cell holds, with its sign: (1,234) and -1,234 are negative. None if the cell is not a number."""
    s = cell.strip()
    t = FOOT.sub("", s).strip()
    if t and re.search(r"\d", t):                     # footnote marks after a number; "( 228 )" alone is the number
        s = t
    s = re.sub(r"^\$\s*|\s*\$$", "", s).replace("$", "").strip()
    neg = False
    if re.fullmatch(r"\(\s*[^()]*\s*\)", s):
        neg, s = True, s[1:-1].strip()
    if s[:1] in "-−–" and len(s) > 1:
        neg, s = True, s[1:].strip()
    s = s.replace("$", "").strip()
    if not re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?", s):
        return None
    v = float(s.replace(",", ""))
    return -v if neg else v


def same(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-6 * max(1.0, abs(b))


def unit_factor(words: str) -> float:
    w = words.lower()
    if "million" in w:
        return 1e6
    if "thousand" in w or re.search(r"0{3}", w):
        return 1e3
    return 1.0


def date_forms(iso: str) -> list:
    d = dt.date.fromisoformat(iso[:10])
    m = MONTHS[d.month - 1]
    return [f"{m} {d.day}, {d.year}", f"{m} {d.day},{d.year}", f"{m} {d.day} {d.year}", f"{d.day} {m} {d.year}", f"{m[:3]}. {d.day}, {d.year}",
            f"{m[:3]} {d.day}, {d.year}", f"{d.month}/{d.day}/{d.year}", f"{d.month:02d}/{d.day:02d}/{d.year}", d.isoformat()]


def find_date(text: str, iso: str, near_end=True):
    """(start, end) of the last (or first) printed form of the date in text, any letter case."""
    best = None
    low = text.lower()
    for f in date_forms(iso):
        for m in re.finditer(re.escape(f.lower()).replace(r"\ ", r"\s*"), low):
            if best is None or (m.start() > best[0] if near_end else m.start() < best[0]):
                best = (m.start(), m.end())
    return best


class Filing:
    def __init__(self, path: Path):
        t = path.read_text(encoding="utf-8", errors="ignore")
        self.t = re.sub(r"</?(?:table|tr|td|th)\b", lambda x: x[0].lower(), t, flags=re.I)   # same text the readers used
        self._doc = None
        self._vis = None

    @property
    def doc(self):
        if self._doc is None:
            self._doc = PR.Doc(self.t)
        return self._doc

    def _build(self):
        """The visible text of the whole filing (hidden parts and tags removed, entities decoded, spaces collapsed) and,
        for each piece, where it starts in the raw file and in the visible text."""
        d, t = self.doc, self.t
        pieces, p = [], 0
        for a, b in d.hid + [(len(t), len(t))]:
            if a > p:
                pieces.append((p, t[p:a]))
            p = max(p, b)
        raw_at, vis_at, out, n = [], [], [], 0
        for start, seg in pieces:
            for m in re.finditer(r"<[^>]*>|[^<]+", seg):
                if m[0].startswith("<"):
                    continue
                txt = re.sub(r"\s+", " ", html.unescape(m[0]).replace("\xa0", " ").replace("\u200b", " ").replace("\u200c", ""))
                if not txt.strip():
                    if out and not out[-1].endswith(" "):
                        out.append(" "); n += 1
                    continue
                if out and not out[-1].endswith(" "):
                    out.append(" "); n += 1
                raw_at.append(start + m.start()); vis_at.append(n)
                out.append(txt.strip()); n += len(txt.strip())
        self._vis, self._raw_at, self._vis_at = "".join(out), raw_at, vis_at

    def vpos(self, raw: int) -> int:
        """Position in the visible text of the raw position (the start of the text piece at or after it)."""
        import bisect
        if self._vis is None:
            self._build()
        i = bisect.bisect_left(self._raw_at, raw)
        return self._vis_at[i] if i < len(self._vis_at) else len(self._vis)

    def vis(self) -> str:
        if self._vis is None:
            self._build()
        return self._vis

    def visible(self, a: int, b: int) -> str:
        """Visible text between two raw positions."""
        return self.vis()[self.vpos(a):self.vpos(b)].strip()

    def row_cells(self, table: int, row: int) -> tuple:
        """(cells of the row as printed, position of the row). The issuer alone on its row takes the next row's cells too."""
        z = self.t.find("</table>", table)
        s = self.t[table:z if z > 0 else len(self.t)]
        trs = list(re.finditer(r"<tr\b[^>]*>(.*?)</tr>", s, re.S | re.I))
        if row >= len(trs):
            return [], table
        def cell(tr):
            cs = [c for c in (clean(x) for x in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", tr, re.S | re.I)) if c]
            out = []
            for c in cs:                                 # "(62,733" and ")" in two cells are one printed number
                if out and re.fullmatch(r"\)\s*%?", c) and out[-1].count("(") > out[-1].count(")"):
                    out[-1] += c
                else:
                    out.append(c)
            return out
        cs = cell(trs[row][1])
        if len(cs) == 1 and row + 1 < len(trs):
            nx = cell(trs[row + 1][1])
            if any(num(c) is not None for c in nx):
                cs += nx
        return cs, table + trs[row].start()

    def table_ends(self) -> list:
        if not hasattr(self, "_ends"):
            self._ends = [m.end() for m in re.finditer(r"</table>", self.t)]
        return self._ends

    def table_spans(self) -> list:
        """(start, end) of every outermost table, in visible-text positions."""
        if not hasattr(self, "_spans"):
            out, depth, a = [], 0, 0
            for m in re.finditer(r"<(/?)table\b", self.t):
                if not m[1]:
                    if depth == 0:
                        a = m.start()
                    depth += 1
                elif depth:
                    depth -= 1
                    if depth == 0:
                        out.append((self.vpos(a), self.vpos(m.end())))
            self._spans = out
        return self._spans

    def ix_values(self, ctx: str) -> dict:
        """{concept: (printed text of the tag, printed text of its table cell, sign attr, scale attr)} for one context."""
        out = {}
        for m in re.finditer(r"<ix:nonFraction([^>]*)>(.*?)</ix:nonFraction>", self.t, re.S):
            at = m[1]
            if f'contextRef="{ctx}"' not in at:
                continue
            nm = re.search(r'name="([^"]+)"', at)[1].split(":")[-1]
            a = self.t.rfind("<td", 0, m.start())
            b = self.t.find("</td>", m.end())
            cellt = clean(self.t[a:b]) if a >= 0 and b > 0 and self.t.rfind("</td>", a, m.start()) < 0 else ""
            sg = re.search(r'sign="(-)"', at)
            sc = re.search(r'scale="(-?\d+)"', at)
            out[nm] = (clean(m[2]), cellt, "-" if sg else "", int(sc[1]) if sc else 0, m.start())
        return out

    def row_at(self, pos: int) -> list:
        """The printed cells of the table row (<tr>) that holds the raw position, or [] when it is not inside a row."""
        a = self.t.rfind("<tr", 0, pos)
        if a < 0 or self.t.rfind("</tr>", a, pos) >= 0:
            return []
        b = self.t.find("</tr>", pos)
        if b < 0:
            return []
        out = []
        for c in (clean(x) for x in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", self.t[a:b + 5], re.S | re.I)):
            if not c:
                continue
            if out and re.fullmatch(r"\)\s*%?", c) and out[-1].count("(") > out[-1].count(")"):
                out[-1] += c
            else:
                out.append(c)
        return out

    def ix_instant(self, ctx: str) -> str:
        m = re.search(rf'<xbrli:context id="{re.escape(ctx)}">(.*?)</xbrli:context>', self.t, re.S)
        if not m:
            return ""
        i = re.search(r"<xbrli:(instant|endDate)>([^<]+)<", m[1])
        return f'{i[2].strip()}  (the {"instant" if i[1] == "instant" else "end"} date of iXBRL context {ctx})' if i else ""


def unit_words(f: Filing, header: str, pos: int, table: int | None) -> tuple:
    """(the printed words that set the scale, where they are), looked for in this order:
    the column header; the rows of the same table above the row and the title just above the table (up to 1,500
    characters, never past another table); the heading of the schedule the row is in (from 300 characters before the
    last "Schedule of Investments"-type heading above the row), outside every table. Words inside other tables
    ("Notional Amount (000)" of a swaps table) and headings of earlier parts of the filing never count."""
    m = UNIT.search(header or "")
    if m:
        return m[0].strip(), "column header"
    m = DOLLARS.search(header or "")
    if m:
        return m[0].strip(), "column header: whole dollars"
    v = f.vis()
    e = f.vpos(pos)
    if table is not None:
        import bisect
        ends = f.table_ends()
        i = bisect.bisect_left(ends, table) - 1
        va = f.vpos(table)
        lo = max(f.vpos(ends[i]) if i >= 0 else 0, va - 1500)
        ms = list(UNIT.finditer(v, lo, e))
        if ms:
            return ms[-1][0].strip(), "same table or its title"
    spans = f.table_spans()
    lo = max(0, e - 300000)
    sc = list(SCHED_HEAD.finditer(v, lo, e))                 # words above the schedule's own heading belong to an earlier part
    if sc:
        lo = max(lo, sc[-1].start() - 300)
    for m in reversed(list(UNIT.finditer(v, lo, e))):
        if not any(a <= m.end() - 1 < b for a, b in spans):
            return m[0].strip(), "heading of the schedule, outside any table"
    return "", ""


def asof_proof(f: Filing, e: dict, iso: str, pos: int) -> tuple:
    """(source, the printed words that hold the date) or (source, "") when the date cannot be shown in the filing."""
    src = e["as_of_source"]
    if not iso:
        return src, ""
    if src == "iXBRL context date":                    # the date printed on the page comes first; the context is machine data
        seg = heading_date(f, iso, pos)
        if seg:
            return "schedule heading (the same date as the iXBRL context of the tags)", seg
        return "iXBRL context of the tags", f.ix_instant(e["table"]) if iso in f.ix_instant(e["table"]) else ""
    if src.startswith("reporting period on the cover of the N-CSR"):
        tag = re.search(r'<ix:nonNumeric[^>]*name="dei:DocumentPeriodEndDate"[^>]*>(.*?)</ix:nonNumeric>', f.t[:3000000], re.S)
        if tag and find_date(clean(tag[1]), iso):
            return "dei:DocumentPeriodEndDate tag of the filing", f'Document period end date: {clean(tag[1])}'
        z = f.visible(0, 600000)
        m = re.search(r"date\s+of\s+reporting\s+period\W{0,20}(.{0,200})", z, re.I)
        if m and find_date(m[0], iso):
            a, b = find_date(m[0], iso, near_end=False)     # the first date after the words, not a later repeat of it
            return "N-CSR cover", m[0][:b].strip()
        return "N-CSR cover", ""
    return "schedule heading", heading_date(f, iso, pos)


def heading_date(f: Filing, iso: str, pos: int) -> str:
    """The printed words of the nearest schedule heading above the raw position that carry the date, or ""."""
    v = f.vis()
    e = f.vpos(pos)
    hits = []
    for form in date_forms(iso):
        for m in re.finditer(re.escape(form).replace(r"\ ", r"\s*"), v[:e], re.I):
            hits.append((m.start(), m.end()))
    for a, b in sorted(hits, reverse=True)[:400]:
        near = v[max(0, a - 400):b + 400]
        if SCHED.search(near):
            before = list(SCHED.finditer(v, max(0, a - 400), a))
            if before:                                  # from the heading words to the date: "Schedule of Investments As of March 31, 2025"
                h = before[-1]
                seg = v[h.start():b + 60] if a - h.end() <= 200 else v[h.start():h.end()] + " … " + v[a:b + 60]
            else:                                       # the heading words come after the date: "December 31, 2025 ... Schedule of Investments"
                after = SCHED.search(v, b, b + 400)
                seg = (v[a:after.end()] if after.start() - b <= 200 else v[a:b] + " … " + v[after.start():after.end()]) if after else v[a:b + 60]
            return seg.strip()
    return ""


_MARK = None


def per_share(h: dict, co: str) -> str:
    """Second, independent proof of the scale for shares: value / shares next to the median price the funds report in
    N-PORT for that month (within 3x either way). Thousands-for-dollars would be 1,000x off."""
    global _MARK
    if _MARK is None:
        import json
        mk = px.INPUTS / co / "data" / "nport_price_monthly.csv"
        cfg = json.loads(px.config_path(co).read_text(encoding="utf-8"))
        _MARK = ({r["month"]: float(r["median_price"]) for r in csv.DictReader(open(mk, encoding="utf-8"))} if mk.exists() else {}, cfg)
    mark, cfg = _MARK
    try:
        sh, v, d = float(h["shares"]), float(h["fair_value_usd"]), h["as_of_date"]
    except (TypeError, ValueError):
        return ""
    if not (sh and v and d and mark) or h["position_type"] != "equity":
        return ""
    k = min(mark, key=lambda x: abs(int(x[:4]) * 12 + int(x[5:]) - int(d[:4]) * 12 - int(d[5:7])))
    ratio = cfg.get("split_ratio", 1) if d < cfg.get("split_date", "0") else 1
    p = v / sh / ratio
    ok = mark[k] / 3 <= p <= mark[k] * 3
    return f"{'ok' if ok else 'FAIL'}: value per share ${v / sh:,.2f}" + (f" (/{ratio} for the split)" if ratio != 1 else "") + f", N-PORT median price {k} ${mark[k]:,.2f}"


def prove(f: Filing, h: dict, e: dict, co: str = "databricks") -> dict:
    fields = [("qty", h["shares"] or h["par"], e.get("qty_factor") or e.get("unit_factor") or "1"), ("cost", h["cost_usd"], e.get("unit_factor") or "1"),
              ("value", h["fair_value_usd"], e.get("unit_factor") or "1"), ("commitment", h["commitment_usd"], e.get("unit_factor") or "1")]
    out = {k + "_w": "" for k, _, _ in fields}
    why = []
    meth = e["method"]
    if meth == "iXBRL tag":
        ix = f.ix_values(e["table"])
        concept = {"qty": ("InvestmentOwnedBalancePrincipalAmount", "InvestmentOwnedBalanceShares"), "cost": ("InvestmentOwnedAtCost",),
                   "value": ("InvestmentOwnedAtFairValue",), "commitment": ()}
        pos = min((v[4] for v in ix.values()), default=0)
        for k, pv, _ in fields:
            if pv in ("", None):
                continue
            c = next((ix[x] for x in concept[k] if x in ix), None)
            if not c:
                why.append(f"{k}: no tag in the filing")
                continue
            txt, cellt, sg, sc, _ = c
            n = num(txt)
            shown = cellt or txt
            out[k + "_w"] = shown + (f'  [tag: sign="-"]' if sg else "") + (f'  [tag: scale={sc}]' if sc else "")
            if n is None or not same((-n if sg else n) * 10 ** sc, float(pv)):
                why.append(f"{k}: tag gives {txt} sign {sg or '+'} scale {sc}, pipeline {float(pv):,.0f}")
            elif sg and num(shown) is not None and num(shown) > 0:
                out[k + "_w"] += "  (the page prints it without a minus; the tag marks it negative)"
        unit = "iXBRL scale attribute of each tag"
        fv = next((ix[x] for x in ("InvestmentOwnedAtFairValue", "InvestmentOwnedAtCost", "InvestmentOwnedBalancePrincipalAmount") if x in ix), None)
        row = f.row_at(fv[4]) if fv else []             # the table row as printed on the page, the tags sit inside its cells
        if row and all(v[1] in row or v[0] in " | ".join(row) for k, v in ix.items() if k in ("InvestmentOwnedAtFairValue", "InvestmentOwnedAtCost")):
            ground = " | ".join(row)
        else:
            why.append("the tagged numbers are not inside a printed table row")
            ground = ""
        if fv:                                          # the scale words printed on the page, when they give the same scale as the tag
            ta = f.t.rfind("<table", 0, fv[4])
            words, where = unit_words(f, "", fv[4], ta if ta >= 0 else None)
            if words and unit_factor(words) == 10 ** fv[3]:
                unit = f"{words}  [{where}; the same scale as the iXBRL scale attribute of the tags]"
            elif not words and fv[3] == 0:
                unit = "no scale words printed for this table (whole dollars); the iXBRL tags carry no scale either"
    else:
        if meth == "table":
            cells, pos = f.row_cells(int(e["table"]), int(e["row"]))
        else:                                           # text layout: the visible text of the block at the name
            pos = int(e["row"])
            txt = f.vis()[f.vpos(pos):f.vpos(pos) + 1400]
            ms = list(re.finditer(r"(?<![\d/])\(?\$?\s?\d[\d,]*(?:\.\d+)?\)?(?![\d/])|[^\s\d($]+(?:\s+[^\s\d($]+)*", txt))[:60]
            cells = [m[0] for m in ms]
        used, last = set(), -1
        for k, pv, fac in fields:
            if pv in ("", None):
                continue
            fac = float(fac or 1)
            inner = [m for c in (cells if meth == "table" else [" ".join(cells)])      # a laid-out page splits "cost" and "$88,431)"
                     for m in re.finditer(r"\bcost\b[^$\d]{0,12}\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)", c, re.I)
                     if same(float(m[1].replace(",", "")) * fac, float(pv))]
            if k == "cost" and inner:                     # cost printed inside the security's name: "(Acquired 8/31/21, Cost $3,616,301)"
                out[k + "_w"] = inner[0][0]
                continue
            if k == "cost" and meth != "table":          # one cost split over two boxes of a laid-out page: "cost $3,030,0 10 )"
                well = re.compile(r"\d{1,3}(?:,\d{3})+|\d{1,3}")
                split = [m for m in re.finditer(r"\bcost\b[^$\d]{0,12}\$\s*(\d[\d,]*)\s+(\d{1,3})(?=\s*\))", " ".join(cells), re.I)
                         if not well.fullmatch(m[1]) and well.fullmatch(m[1] + m[2]) and same(float((m[1] + m[2]).replace(",", "")) * fac, float(pv))]
                if split:
                    out[k + "_w"] = split[0][0] + "  (one number printed in two boxes)"
                    continue
            hits = [i for i, c in enumerate(cells) if i not in used and num(c) is not None and same(num(c) * fac, float(pv))]
            right = [i for i in hits if i > last]
            if not hits:
                why.append(f"{k}: no cell of the row times {fac:g} equals the pipeline {float(pv):,.0f}")
                continue
            i = (right or hits)[0]
            used.add(i); last = i
            out[k + "_w"] = cells[i]
        words, where = unit_words(f, e.get("header", ""), pos, int(e["table"]) if meth == "table" else None)
        uf = float(e.get("unit_factor") or 1)
        if not words:
            if uf != 1:
                why.append(f"scale x{uf:g} used but no printed words for it were found above the row")
            unit = "no scale words printed for this table (whole dollars)"
        elif unit_factor(words) != uf and uf == 1 and per_share(h, co).startswith("ok"):
            unit = (f"no scale words in this table's own header; whole dollars, proven by the value per share (the words "
                    f"\"{words}\" printed above belong to another table)")
        else:
            unit = f"{words}  [{where}]"
            if unit_factor(words) != uf:
                why.append(f"scale words say x{unit_factor(words):g}, pipeline used x{uf:g}")
        if meth == "table":
            ground = " | ".join(cells)
        else:                                           # the printed text from the name to the last number read, as it reads
            ground = txt[:ms[max(used)].end() if used else 400][:600].strip()
    ps = per_share(h, co)
    if ps.startswith("FAIL"):
        why.append("value per share is far from the N-PORT price: " + ps[6:])
    src, proof = asof_proof(f, e, h["as_of_date"], pos)
    if not proof:
        why.append(f"date {h['as_of_date']}: not found as printed in the {src}")
    out.update(unit_w=unit, scale_check=ps, asof_src=src, asof_w=proof, ground=ground,
               match="Yes: every number, the scale and the date are as written in the filing" if not why else "No: " + "; ".join(why))
    return out


def main():
    co = sys.argv[1]
    ev = px.OUT / co / "evidence"
    E = {r["evidence_id"]: r for r in csv.DictReader(open(ev / "evidence_rows.csv", encoding="utf-8"))}
    H = list(csv.DictReader(open(ev / "holdings_from_evidence.csv", encoding="utf-8")))
    PATHS = {r["sec_url"]: px.DUMPS / m.name.replace("_manifest.csv", "") / r["file"] for m in px.DUMPS.glob(f"{co}*_manifest.csv")
             for r in csv.DictReader(open(m, encoding="utf-8"))}
    order = sorted(range(len(H)), key=lambda i: H[i]["sec_url"])
    res = [None] * len(H)
    cur, f = None, None
    for n, i in enumerate(order):
        h = H[i]
        e = E[h["evidence_id"]]
        if h["sec_url"] != cur:
            cur, f = h["sec_url"], Filing(PATHS[h["sec_url"]])
        res[i] = {"evidence_id": h["evidence_id"], **prove(f, h, e, co)}
        if n % 300 == 0:
            print("proved", n, "of", len(H), flush=True)
    cols = list(res[0].keys()) if res else ["evidence_id", "match"]     # a company with no holdings: an empty proof file
    w = csv.DictWriter(open(ev / "written_proof.csv", "w", newline="", encoding="utf-8"), cols)
    w.writeheader(); w.writerows(res)
    import collections
    print(collections.Counter(r["match"][:3] for r in res))


if __name__ == "__main__":
    main()
