"""Company-finder verdicts per filing, for the steps that find the company by its name pattern alone (evidence tables).

A filing is a look-alike when company_finder.py dropped every mention of the name in it (its --llm-disambiguate check, or the
name-tail rule when switched on, said the name belongs to another organisation, e.g. "Horizon3 Investment Management LLP" for
Horizon3 AI) and kept none. A filing the finder never saw has no verdict and keeps its rows.

Reads the finder's output for each dump folder from the finder output folders (paths.finder_run_dirs: <PX_FINDER_ROOT>/out/<folder>*/
or <PX_FINDER_ROOT>/<run>/out/<folder>_*/), in either naming: <folder>.csv + <folder>_dropped.csv (--out <folder>.csv), or
<folder>_mentions.csv + <folder>_mentions_dropped.csv (--out <folder>_mentions.csv, the README's command).
"""
from __future__ import annotations

import collections
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # pipeline/: paths.py
import paths as P  # noqa: E402

ACC = re.compile(r"(\d{10}-\d{2}-\d{6})")
csv.field_size_limit(10**8)


def verdicts(folders: list) -> dict:
    """accession -> {"kept": n, "dropped": n, "names": Counter of the longer names the dropped mentions sat in}."""
    out: dict = collections.defaultdict(lambda: {"kept": 0, "dropped": 0, "names": collections.Counter()})
    for f in folders:
        for d in P.finder_run_dirs(f):
            for stem in (f, f"{f}_mentions"):
                for name, kind in ((f"{stem}.csv", "kept"), (f"{stem}_dropped.csv", "dropped")):
                    p = d / name
                    if not p.exists():
                        continue
                    with open(p, newline="", encoding="utf-8") as fh:
                        for r in csv.DictReader(fh):
                            m = ACC.search(Path(r.get("document") or "").name)
                            if not m:
                                continue
                            e = out[m.group(1)]
                            e[kind] += int(r.get("mention_count") or 1) if kind == "kept" else 1
                            if kind == "dropped" and r.get("longer_name"):
                                e["names"][r["longer_name"]] += 1
    return dict(out)


def lookalikes(folders: list) -> dict:
    """accession -> short reason, for filings whose every finder mention was dropped. Prints how many filings had a verdict, so an empty
    result is visible (no finder output found) rather than silent."""
    v = verdicts(folders)
    look = {acc: "look-alike name: company-finder dropped all {} mention(s) in this filing{}".format(
                e["dropped"], f" (e.g. {e['names'].most_common(1)[0][0]})" if e["names"] else "")
            for acc, e in v.items() if e["dropped"] and not e["kept"]}
    print(f"company-finder verdicts: {len(v)} filing(s) seen in {', '.join(folders)}; {len(look)} look-alike filing(s)"
          + ("" if v else f" (no finder output found under {P.FINDER_ROOT}: no filing is set aside)"), flush=True)
    return look
