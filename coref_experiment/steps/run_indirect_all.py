#!/usr/bin/env python3
"""Indirect mentions (it / its / they ...) of one company over all its prepared filings. Only pronouns within --window
paragraphs of a mention of the company go to the model (--filer-implicit off); the filer's own we/our/the Company are
not sent. Runs filings in parallel; a filing already done is skipped.

    python run_indirect_all.py <company folder prefix> <out dir> [--parallel 4] [--only <file listing .meta.json paths>]
"""
import glob, json, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
co, out = sys.argv[1], Path(sys.argv[2])
par = int(sys.argv[sys.argv.index("--parallel") + 1]) if "--parallel" in sys.argv else 4
D = Path(__file__).resolve().parents[1] / "coref_verifier" / "data"
metas = sorted(glob.glob(str(D / "companies" / f"{co}*__*_mentions.meta.json")))
if "--only" in sys.argv:                               # a chosen subset, e.g. a test sample
    keep = {Path(x).name for x in open(sys.argv[sys.argv.index("--only") + 1]).read().split()}
    metas = [x for x in metas if Path(x).name in keep]
out.mkdir(parents=True, exist_ok=True)
log = open(out / "run.log", "a")


def one(mf):
    m = json.load(open(mf))
    pdf = D / "pdfs" / m["document"]
    if (out / f"{Path(m['document']).stem}_indirect_run.json").exists():
        return "skip"
    r = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "indirect_mentions.py"), str(pdf), "--mentions", mf.replace(".meta.json", ".csv"), "--filer", m["filer"],
                        "--filer-implicit", "off", "--out-dir", str(out), "--workers", "4", "--rpm", "60"], capture_output=True, text=True, timeout=3600)
    log.write(f"== {m['form']} {m['accession']} rc={r.returncode}\n{r.stdout[-600:]}\n{r.stderr[-400:] if r.returncode else ''}\n")
    log.flush()
    return "ok" if r.returncode == 0 else "fail"


import collections
c = collections.Counter()
with ThreadPoolExecutor(par) as pool:
    for i, res in enumerate(pool.map(one, metas)):
        c[res] += 1
        if i % 100 == 0:
            print(i, "of", len(metas), dict(c), flush=True)
print("done", dict(c), flush=True)
