#!/usr/bin/env python3
"""Compare every numeric cell of our model CSVs with the reference CSVs of the same name. Prints a count per file and each mismatch."""
import argparse
import csv
import re
from pathlib import Path


def n(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


ap = argparse.ArgumentParser()
ap.add_argument("ours")
ap.add_argument("reference")
ap.add_argument("--tol", type=float, default=0.0005, help="relative tolerance")
ap.add_argument("--show", type=int, default=6)
ap.add_argument("--report", default="")
a = ap.parse_args()
tot, ok, lines = 0, 0, []
for p in sorted(Path(a.ours).glob("*.csv")):
    q = Path(a.reference) / p.name
    if not q.exists():
        continue
    A = list(csv.DictReader(open(p, encoding="utf-8")))
    B = list(csv.DictReader(open(q, encoding="utf-8")))
    key = list(B[0])[0] if B else ""
    if key not in (A[0] if A else {}):
        print("KEY MISSING", p.name)
        continue
    bk = {re.sub(r"\s+", " ", str(r[key]).split(" (")[0]): r for r in B}
    t, g, bad = 0, 0, []
    for i, r in enumerate(A):
        o = B[i] if len(A) == len(B) else bk.get(re.sub(r"\s+", " ", str(r[key]).split(" (")[0]))
        if o is None:
            bad.append(f"row {r[key]} not in reference")
            continue
        for c, v in r.items():
            if c in o and n(o[c]) is not None:
                t += 1
                x, y = n(v), n(o[c])
                same = x is not None and (abs(x - y) <= max(1.01, abs(y) * a.tol) if abs(y) > 100 else abs(x - y) <= max(1e-3, abs(y) * a.tol))
                g += same
                if not same:
                    bad.append(f"{r[key]} | {c}: ours {v}, reference {o[c]}")
    tot, ok = tot + t, ok + g
    print(f"{p.name:44} {g}/{t}")
    lines += [f"{p.name},{g},{t}"]
    for b in bad[:a.show]:
        print("    ", b[:150])
print(f"TOTAL {ok}/{tot} = {ok / max(tot, 1):.1%}")
if a.report:
    Path(a.report).write_text("file,matched,compared\n" + "\n".join(lines) + f"\nTOTAL,{ok},{tot}\n")
