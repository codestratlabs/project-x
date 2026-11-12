#!/usr/bin/env python3
"""Check every extracted number against the filing it came from. No model is used.

Test 1, nothing missed: every table row or sentence naming the company with a number is listed and compared with the evidence rows.
Test 2, each number right: printed in the filing, unit from the filing's own table header, per-share sense check, second source.
    Verified          printed, unit agrees, and a second source agrees (N-PORT or an iXBRL tag in the filing)
    Filing-confirmed  printed and the unit agrees, no second source
    Failed            anything else, with the reason

    python evidence_check.py databricks --folders databricks databricks_ncsr databricks_delta
Writes out/<company>/evidence/.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import csv
import datetime as dt
import hashlib
import html
import json
import multiprocessing as mp
import re
from pathlib import Path

csv.field_size_limit(10**8)
HERE = Path(__file__).parent
NUM = re.compile(r"(?<![\d,./-])\(?\$?\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d{3,})\)?(?![\d/]|-\d)")
UNIT = re.compile(r"(?:\bin\s|\$\s|expressed in\s|amounts?\s)(?:\s|&nbsp;|&#160;|<[^>]+>|U\.S\.\s|dollars\s|USD\s){0,12}(thousands|millions)\b|\(000[’'`]?s?\)|\(\$\s?000[’'`]?s?\)|\$\s?000[’'`]?s\b", re.I)


def clean(x: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", x)).replace("\xa0", " ").replace("​", "").replace("‌", "")).strip()


def nums(text: str) -> list:
    res = []
    for m in NUM.finditer(text):
        v = float(m[1].replace(",", ""))
        if m[1].isdigit() and 1900 <= v <= 2100:
            continue                           # a year
        res.append(v)
    return res


def tagged(t: str, key: str) -> set:
    """Values tagged in iXBRL for contexts naming the company, already scaled by the filing."""
    ctx = {c for c, b in re.findall(r'<xbrli:context id="([^"]+)">(.*?)</xbrli:context>', t, re.S) if re.search(key, b, re.I)}
    res = set()
    if ctx:
        for attrs, val in re.findall(r"<ix:nonFraction([^>]*)>(.*?)</ix:nonFraction>", t, re.S):
            c = re.search(r'contextRef="([^"]+)"', attrs)
            if c and c[1] in ctx and 'unitRef="usd"' in attrs.lower():
                txt = re.sub(r"<[^>]+>", "", val).replace(",", "").strip()
                if re.fullmatch(r"\d*\.?\d+", txt):
                    sc = re.search(r'scale="(-?\d+)"', attrs)
                    res.add(round(float(txt) * 10 ** int(sc[1] if sc else 0)))
    return res


def scan(job: tuple) -> dict:
    path, key, quotes, cache = job
    p = Path(path)
    c = Path(cache) / (hashlib.sha1(path.encode()).hexdigest() + ".json")
    if c.exists():
        return json.loads(c.read_text(encoding="utf-8"))
    res = {"path": path, "lines": [], "tags": [], "quotes": {}, "error": ""}
    try:
        if p.suffix.lower() == ".pdf":
            import pypdfium2                   # lighter on memory than a layout parser
            doc = pypdfium2.PdfDocument(str(p))
            pages = []
            for i in range(len(doc)):
                pg_ = doc[i]
                tp = pg_.get_textpage()
                pages.append(tp.get_text_range())
                tp.close()
                pg_.close()
            doc.close()
            whole = "\n".join(pages)
            for pg in pages:
                u = UNIT.search(pg)
                unit = None if not u else (1e6 if (u[1] or "").lower() == "millions" else 1e3)
                for ln in pg.splitlines():
                    if re.search(key, ln, re.I):
                        res["lines"].append(("row", ln[:300], nums(ln), unit))
            norm = re.sub(r"[^a-z0-9]", "", whole.lower())
        else:
            t = p.read_text(encoding="utf-8", errors="ignore")
            units = [(m.start(), 1e6 if (m[1] or "").lower() == "millions" else 1e3) for m in UNIT.finditer(t)]
            pos = [u[0] for u in units]
            seen = set()
            for m in re.finditer(key, t, re.I):
                a = t.rfind("<tr", max(0, m.start() - 6000), m.start())
                z = t.find("</tr>", m.end(), m.end() + 12000)
                closed = t.rfind("</tr>", a, m.start()) if a >= 0 else 0
                if a >= 0 and z > 0 and closed < 0:
                    kind, text = "row", clean(re.sub(r"</t[dh]>", " | ", t[a:z], flags=re.I))
                    k = a
                else:
                    kind, text = "text", clean(t[max(0, m.start() - 250):m.end() + 450])
                    i = text.lower().find(re.search(key, text, re.I)[0].lower()) if re.search(key, text, re.I) else 0
                    k = m.start() // 200
                    text = text[max(0, i - 60):]
                if k in seen:
                    continue
                seen.add(k)
                n = nums(text)
                if n:
                    i = bisect.bisect_right(pos, m.start()) - 1
                    unit = units[i][1] if i >= 0 and m.start() - pos[i] < 120000 else None
                    res["lines"].append((kind, text[:420], n[:14], unit))
            res["tags"] = sorted(tagged(t, key))
            norm = re.sub(r"[^a-z0-9]", "", clean(t).lower()) if quotes else ""
        for q in quotes:
            nq = re.sub(r"[^a-z0-9]", "", q.lower())
            res["quotes"][q] = bool(nq) and (nq in norm or nq[:50] in norm)
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"[:160]
    c.write_text(json.dumps(res), encoding="utf-8")
    return res


def near(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.51, abs(b) * 0.0005)


def limit() -> None:
    """Keep one worker from taking the machine down: a filing too large for this limit is logged as an error."""
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (1400 * 1024 ** 2, 1400 * 1024 ** 2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("company")
    ap.add_argument("--folders", nargs="+", required=True)
    ap.add_argument("--dumps", default=str(HERE.parents[2] / "dumps"))
    a = ap.parse_args()
    cfg = json.loads((HERE / "model_config" / f"{a.company}.json").read_text(encoding="utf-8"))
    key, base, ev = cfg["company"]["name_regex"], HERE / "out" / a.company, HERE / "out" / a.company / "evidence"
    ev.mkdir(parents=True, exist_ok=True)
    day, ratio = cfg.get("split_date", "0"), cfg.get("split_ratio", 1)
    marks = {}
    pm = HERE / "inputs" / a.company / "data" / "nport_price_monthly.csv"
    if pm.exists():
        marks = {r["month"]: float(r["median_price"]) for r in csv.DictReader(open(pm, encoding="utf-8"))}
    files, url = {}, {}
    for f in a.folders:
        m = Path(a.dumps) / f"{f}_manifest.csv"
        for r in csv.DictReader(open(m, encoding="utf-8", newline="")):
            p = Path(a.dumps) / f / r["file"]
            if p.exists() and p.suffix.lower() in (".htm", ".html", ".pdf", ".txt"):
                files[str(p)] = r
                url[r["sec_url"]] = str(p)
    H = list(csv.DictReader(open(base / "holdings.csv", encoding="utf-8")))
    F = list(csv.DictReader(open(base / "facts.csv", encoding="utf-8")))
    quotes = collections.defaultdict(list)
    for r in F:
        if r["sec_url"] in url and r["evidence"]:
            quotes[url[r["sec_url"]]].append(r["evidence"])
    cache = ev / "scan_cache"
    cache.mkdir(exist_ok=True)
    jobs = sorted(((p, key, quotes.get(p, []), str(cache)) for p in files), key=lambda j: Path(j[0]).stat().st_size)
    with mp.Pool(a.workers, initializer=limit, maxtasksperchild=25) as pool:
        scans = {}
        for i, res in enumerate(pool.imap_unordered(scan, jobs, chunksize=1)):
            scans[res["path"]] = res
            if i % 100 == 0:
                print("scanned", i, "of", len(jobs), flush=True)
    errors = [(p, s["error"]) for p, s in scans.items() if s["error"]]

    def mark_at(d: str):
        if not marks or not d:
            return None
        k = min(marks, key=lambda x: abs(int(x[:4]) * 12 + int(x[5:]) - int(d[:4]) * 12 - int(d[5:7])))
        return marks[k]

    def f_(x):
        return float(x) if x not in ("", None) else None

    out = []
    used = collections.defaultdict(set)
    by_filer = collections.defaultdict(list)
    for p_, s_ in scans.items():
        by_filer[files[p_]["filer"]] += s_["lines"]
    for r in H:
        s = scans.get(url.get(r["sec_url"], ""))
        vals = [(n, f_(r[c])) for n, c in (("fair value", "fair_value_usd"), ("cost", "cost_usd"), ("commitment", "commitment_usd")) if f_(r[c])]
        sh = f_(r["shares"])
        why, cross = [], ""
        if not s:
            why.append("source file not held")
        else:
            # a merged row cites its latest filing; the same figure may be printed only in the filer's other filings
            lines = s["lines"] + by_filer.get(files[s["path"]]["filer"], [])
            good = []
            for name, v in vals:
                hit = [(ln, u) for ln in lines for u in (1, 1e3, 1e6) if any(near(p * u, v) for p in ln[2])]
                if not hit:
                    why.append(f"{name} not printed in a line naming the company")
                    continue
                st = [(ln, u) for ln, u in hit if (ln[3] or 1) == u]
                if not st:
                    why.append(f"{name}: unit differs from the table header (stored x{hit[0][1]:g}, header {'x%g' % hit[0][0][3] if hit[0][0][3] else 'none'})")
                    continue
                good += [ln for ln, _ in st]
                for ln, u in st:
                    for p in ln[2]:
                        if near(p * u, v):
                            used[files[s["path"]]["filer"]].add(p)
            if sh and good and not any(near(p, sh) or near(p * 1000, sh) for ln in good for p in ln[2]) and r["position_type"] not in ("funded_loan", "unfunded_commitment"):
                why.append("shares not in the same line as the value")
            if sh:
                used[files[s["path"]]["filer"]].add(sh)
            tag = set(s["tags"])
            if vals and any(any(abs(round(v) - t) <= max(1, t * 0.0005) for t in tag) for _, v in vals):
                cross = "iXBRL tag in the filing"
        fv = f_(r["fair_value_usd"])
        if sh and fv and r["position_type"] == "equity":
            d = r["as_of_date"] or r["filing_date"]
            m = mark_at(d)
            pr = fv / sh / (ratio if d < day else 1)
            if m and not m / 3 <= pr <= m * 3:
                why.append(f"per share ${pr:,.2f} is more than 3x from the fund mark ${m:,.2f}")
        nc = r.get("nport_check", "")
        if nc.startswith("value differs"):
            why.append("differs from N-PORT")
        elif nc.startswith("value matches"):
            cross = "Form N-PORT"
        status = "Failed" if why or not vals else "Verified" if cross else "Filing-confirmed"
        out.append(dict(kind="holding", status=status, reason="; ".join(why) or ("no value" if not vals else ""), second_source=cross, name=r["holder"], item=r["security"], shares=r["shares"], cost=r["cost_usd"], fair_value=r["fair_value_usd"],
                        commitment=r["commitment_usd"], as_of=r["as_of_date"], form=r["form"], filer=r["filer"], filing_date=r["filing_date"], url=r["sec_url"], evidence=r["evidence"][:300]))
    for r in F:
        s = scans.get(url.get(r["sec_url"], ""))
        why = []
        if not s:
            why.append("source file not held")
        elif not s["quotes"].get(r["evidence"]):
            why.append("quote not found in the filing")
        n = f_(r["value_number"])
        if n:
            q = r["evidence"].lower()
            ps = nums(r["evidence"]) + [float(x) for x in re.findall(r"\d+\.\d+|\d+", q)]
            mult = (1, 1e3, 1e6, 1e9)
            if not any(near(p * u, n) for p in ps for u in mult):
                why.append("number not in the quote")
            elif not any(near(p, n) for p in ps) and not re.search(r"thousand|million|billion|\bmn\b|\bbn\b|\d\s?[mbk]\b", q):
                why.append("scaled, but the quote states no unit")
        out.append(dict(kind="fact", status="Failed" if why else "Filing-confirmed", reason="; ".join(why), second_source="", name=r["metric"], item=r["value_text"][:160], shares="", cost="", fair_value=r["value_number"], commitment="", as_of=r["as_of_date"],
                        form=r["form"], filer=r["filer"], filing_date=r["filing_date"], url=r["sec_url"], evidence=r["evidence"][:300]))
    head = list(out[0])
    with open(ev / "number_check.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, head)
        w.writeheader()
        w.writerows(out)
    unc = []
    tot = 0
    cited = {url[r["sec_url"]] for r in H + F if r["sec_url"] in url}
    for p, s in scans.items():
        u = used.get(files[p]["filer"], set())
        for ln in s["lines"]:
            big = [x for x in ln[2] if x >= 1000]
            if ln[0] == "row" and not big:
                continue
            if ln[0] == "text" and "$" not in ln[1]:
                continue
            tot += 1
            if not any(near(x, y) for x in big or ln[2] for y in u):
                unc.append([files[p]["form"], files[p]["file_date"], files[p]["filer"][:60], ln[0], "no" if p not in cited else "yes", ln[1][:300], files[p]["sec_url"]])
    with open(ev / "not_in_evidence.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["form", "file_date", "filer", "kind", "filing_in_evidence", "line", "url"])
        w.writerows(sorted(unc, key=lambda x: x[1]))
    c = collections.Counter((r["kind"], r["status"]) for r in out)
    why = collections.Counter(re.sub(r"[\d$,.]+", "#", x.strip())[:70] for r in out if r["status"] == "Failed" for x in r["reason"].split(";"))
    print(json.dumps({"files": len(files), "scan_errors": len(errors), "status": {f"{k[0]} {k[1]}": v for k, v in sorted(c.items())}, "lines_with_numbers": tot, "lines_not_in_evidence": len(unc)}))
    for k, v in why.most_common(16):
        print(v, k)
    (ev / "scan_errors.json").write_text(json.dumps(errors[:200], indent=1))


if __name__ == "__main__":
    main()
