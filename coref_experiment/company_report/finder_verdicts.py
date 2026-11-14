"""Company-finder verdicts per filing, for the steps that find the company by its name pattern alone (evidence tables).

A filing is a look-alike when company_finder.py dropped every mention of the name in it (its --llm-disambiguate check, or the
name-tail rule when switched on, said the name belongs to another organisation, e.g. "Horizon3 Investment Management LLP" for
Horizon3 AI) and kept none. A filing the finder never saw has no verdict and keeps its rows.

Reads the finder's output for each dump folder from Targeted_pdf/*/out/<folder>_*/ (Targeted_pdf in coref_experiment/, in
Resources/, or in the project root), in either naming: <folder>.csv + <folder>_dropped.csv (--out <folder>.csv), or
<folder>_mentions.csv + <folder>_mentions_dropped.csv (--out <folder>_mentions.csv, the README's command).
"""
from __future__ import annotations

import collections
import csv
import re
from pathlib import Path

HERE = Path(__file__).parent
ACC = re.compile(r"(\d{10}-\d{2}-\d{6})")
csv.field_size_limit(10**8)


def _targeted() -> list:
    return [p for p in (HERE.parent / "Targeted_pdf", HERE.parents[1] / "Resources" / "Targeted_pdf", HERE.parents[2] / "Resources" / "Targeted_pdf") if p.exists()]


def verdicts(folders: list) -> dict:
    """accession -> {"kept": n, "dropped": n, "names": Counter of the longer names the dropped mentions sat in}."""
    out: dict = collections.defaultdict(lambda: {"kept": 0, "dropped": 0, "names": collections.Counter()})
    for f in folders:
        for root in _targeted():
            for d in root.glob(f"*/out/{f}_*"):
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
          + ("" if v else " (no finder output found under Targeted_pdf: no filing is set aside)"), flush=True)
    return look
