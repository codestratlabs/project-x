#!/usr/bin/env python3
"""Compare tables built from raw records with the reference tables: row counts and every shared numeric column, by sorted position or key."""
import csv, sys
from pathlib import Path
A, B = Path(sys.argv[1]), Path(sys.argv[2])
KEY = {"nport_price_monthly.csv": ["month"], "nport_loans.csv": ["report_date", "fund_key", "issuer_title", "par_usd"], "fpds_actions.csv": ["piid", "mod"], "uspto_assignments.csv": ["reel", "frame"],
       "FORMD_offerings.csv": ["offering_id"], "public_comps_sec_xbrl.csv": ["company"]}
def num(x):
    try: return float(x)
    except (TypeError, ValueError): return None
tt, gg = 0, 0
for p in sorted(A.glob("*.csv")):
    a, b = list(csv.DictReader(open(p, encoding="utf-8"))), list(csv.DictReader(open(B / p.name, encoding="utf-8")))
    k = KEY[p.name]
    norm = lambda r: tuple(str(round(num(r[x]))) if num(r[x]) is not None and x == "par_usd" else r[x] for x in k)
    bm = {norm(r): r for r in b}
    miss = [r for r in a if norm(r) not in bm]
    extra = len({norm(r) for r in b} - {norm(r) for r in a})
    t, g, bad = 0, 0, []
    for r in a:
        o = bm.get(norm(r))
        if not o: continue
        for c, v in r.items():
            if c in o and c not in k and num(o[c]) is not None:
                t += 1
                same = num(v) is not None and abs(num(v) - num(o[c])) <= max(0.51, abs(num(o[c])) * 1e-4) if abs(num(o[c])) > 100 else num(v) is not None and abs(num(v) - num(o[c])) < 0.006
                g += same
                if not same: bad.append(f"{norm(r)} {c}: {v} vs {o[c]}")
    tt += t; gg += g
    print(f"{p.name:32} rows ours {len(a)} ref {len(b)}; unmatched ours {len(miss)}, ref {extra}; cells {g}/{t}")
    for x in bad[:4] + [f"ours only {norm(m)}" for m in miss[:3]]: print("    ", x[:130])
print(f"TOTAL cells {gg}/{tt}")
