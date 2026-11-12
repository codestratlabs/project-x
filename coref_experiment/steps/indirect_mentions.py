"""Step 1: indirect mentions (we / our / it / they / the Company ...) of the companies in the passed list.

Scope     Only the companies in the pipeline result passed with --mentions (final.json or *_mentions.csv).
Method    1. Code finds EVERY trigger word in the PDF paragraphs (nothing is left to the model to "notice").
          2. First-person words (we, our, us) and defined terms (the Company, the Fund ...) are the filer's by rule.
          3. Third-person words (it, its, they, their, them) are decided by the LLM, only where a listed company is
             mentioned within --window paragraphs, choosing from that closed list or NONE.
          4. Every trigger is written to the audit file with its status, so the verification kit can reconcile
             the counts with the PDF text (verify_indirect.py).
Outputs   steps/out/<pdf>_indirect_mentions.csv      one row per resolved mention (page, paragraph, context ...)
          steps/out/<pdf>_indirect_all_triggers.csv every trigger and what happened to it (audit / verification)
          steps/out/<pdf>_indirect_run.json         settings, counts, tokens, cost, time, rate limits

Usage:
  company-finder/.venv/bin/python coref_experiment/steps/indirect_mentions.py PDF --mentions final.json
        [--model openai/gpt-4.1] [--window 2] [--filer-rows paragraph|occurrence] [--dry-run]
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import shared as s

c = s.c
d = s.d

SYSTEM = """You resolve pronouns in text extracted from a financial/legal PDF.

FILER: {filer} (id {fid}). The filer is the company that submits the document.

Each paragraph is shown as:
  [P<n>|p<page>] text
  TRIGGERS: T1 "it" in "<its sentence>" ; T2 ...      <- the words to resolve (they are all in that paragraph)
  ALLOWED: <id> <name>; <id> <name> ...                <- the only companies you may answer with
Lines starting with [ctx] are neighbouring paragraphs, only for finding who a pronoun points back to.

For every trigger, decide which ALLOWED company the word refers to ("it", "its", "they", "their", "them" standing
for a company), or NONE.

Answer with a company ONLY when the text itself shows the antecedent: the word points back to a noun in the text that
names that company (or to its defined term such as "the Company" / "the Registrant"). Being the only company in
ALLOWED is NOT a reason to choose it.
Answer NONE when:
- the word is a dummy "it" ("it is more likely than not", "it is not practicable", "it is reasonably certain",
  "it is important to note");
- the sentence is a general statement about corporations, companies, directors, stockholders or taxpayers in
  general (law, accounting policy, "a corporation may ... its certificate of incorporation");
- the word refers to a product, technology, data, a person or group of people, a thing or a place;
- the word refers to a company that is not in ALLOWED;
- you cannot tell.
A plural word (they, their, them) names a company only when it stands for that one company alone. When it stands for
several parties, or for parties that are not in ALLOWED (underwriters, customers, directors, affiliates), answer NONE.
Examples: "Zhang was CEO of A until it was acquired by B" -> "it" is A (answer A's id if A is ALLOWED);
"whether it is more likely than not that the assets will be realised" -> NONE.
When unsure, answer NONE with a low confidence.

Return one row for EVERY trigger of every paragraph, in this form:
  para (integer), trigger_no (integer, the number after T), reason (one short sentence naming the antecedent or why
  none), referent_id (an ALLOWED id or "NONE"), confidence (0-100).
Return JSON only: {{"references": [...]}}."""

SCHEMA = {
    "name": "pronoun_referents", "strict": True,
    "schema": {
        "type": "object", "additionalProperties": False, "required": ["references"],
        "properties": {"references": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["para", "trigger_no", "reason", "referent_id", "confidence"],
            "properties": {
                "para": {"type": "integer"}, "trigger_no": {"type": "integer"}, "reason": {"type": "string"},
                "referent_id": {"type": "string"}, "confidence": {"type": "integer"}}}}},
    },
}

ALL_COLS = ["id", "page", "paragraph_no", "trigger", "trigger_type", "start", "sentence", "company", "company_id", "decided_by",
            "status", "confidence", "reason", "context"]
MAIN_COLS = ["page", "paragraph_no", "trigger", "trigger_type", "count", "company", "company_id", "decided_by",
             "confidence", "reason", "sentence", "context"]


def trigger_rows(scope: s.Scope, window: int, skip_tables: bool, implicit: str) -> tuple[list[dict], dict]:
    """All trigger occurrences, with rule decisions applied. Returns (rows, llm_units by paragraph id).
    implicit: where the filer counts as 'mentioned nearby' without its name: 'defined' = a defined term
    (the Company, the Fund...) within the window, 'any' = also we/our/us, 'off' = only its name."""
    fid = scope.cid(scope.filer_id)
    rows, units = [], {}
    cue_types = {"off": set(), "defined": {"defined_term"}, "any": s.FILER_RULE_TYPES}[implicit]
    cue_pids = {p.pid for p in scope.paras if cue_types and any(t["type"] in cue_types for t in s.find_triggers(p.text))}
    for p in scope.paras:
        trig = s.find_triggers(p.text)
        if not trig:
            continue
        table = skip_tables and d.looks_like_table(p.text)
        near = s.listed_near(scope, p.pid, window)
        for n, t in enumerate(trig, 1):
            r = {"page": p.page, "paragraph_no": p.pid, "trigger": t["word"], "trigger_type": t["type"],
                 "start": t["start"], "no": n, "sentence": s.sentence_at(p.text, t["start"]),
                 "company": "", "company_id": "", "decided_by": "", "status": "", "confidence": "", "reason": "",
                 "context": p.text[:1500]}
            if table:
                r.update(status="table_skipped", decided_by="rule", reason="number-heavy table paragraph")
            elif t["type"] in s.FILER_RULE_TYPES:
                r.update(status="resolved", decided_by="rule", company=scope.filer_name, company_id=fid,
                         confidence=100, reason=f"first-person word or defined term in the filer's own document ({t['type']})")
            elif not near and not any(q in cue_pids for q in range(p.pid - window, p.pid + window + 1)):
                r.update(status="no_listed_company_nearby", decided_by="rule",
                         reason=f"no listed company mentioned within {window} paragraphs")
            else:
                units.setdefault(p.pid, []).append(r)
            rows.append(r)
    return rows, units


def build_chunks(scope: s.Scope, units: dict, window: int, chunk_tokens: int) -> list[dict]:
    fid = scope.cid(scope.filer_id)
    chunks, cur, used = [], {"text": [], "allowed": {}, "pids": []}, 0
    for pid in sorted(units):
        p = scope.by_id[pid]
        near = s.listed_near(scope, pid, window)
        allowed = {scope.cid(k): v for k, v in near.items()}
        allowed[fid] = scope.filer_name
        before, after = s.context_lines(scope, pid)
        tl = "; ".join(f'T{r["no"]} "{r["trigger"]}" in "{r["sentence"][:160]}"' for r in units[pid])
        al = "; ".join(f"{k} {v}{' (the filer)' if k == fid else ''}" for k, v in allowed.items())
        block = "\n".join(x for x in (before, p.line(), f"TRIGGERS: {tl}", f"ALLOWED: {al}", after) if x)
        t = c.est_tokens(block)
        if cur["pids"] and used + t > chunk_tokens:
            chunks.append(cur)
            cur, used = {"text": [], "allowed": {}, "pids": []}, 0
        cur["text"].append(block)
        cur["allowed"][pid] = set(allowed)
        cur["pids"].append(pid)
        used += t
    if cur["pids"]:
        chunks.append(cur)
    return chunks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--mentions", required=True, help="pipeline result: final.json or *_mentions.csv")
    ap.add_argument("--filer", default="")
    ap.add_argument("--model", default=c.DEFAULT_MODEL)
    ap.add_argument("--window", type=int, default=2, help="paragraphs around a trigger searched for listed companies")
    ap.add_argument("--filer-implicit", choices=["defined", "any", "off"], default="defined",
                    help="when a third-person word may point to the filer without its name nearby: a defined term "
                         "(the Company ...) within the window (default), also we/our/us, or never")
    ap.add_argument("--filer-rows", choices=["paragraph", "occurrence"], default="paragraph",
                    help="main CSV: one row per paragraph for the filer's we/our/the Company, or one per occurrence")
    ap.add_argument("--min-confidence", type=int, default=50)
    ap.add_argument("--chunk-tokens", type=int, default=4000)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--rpm", type=int, default=18)
    ap.add_argument("--max-tokens", type=int, default=8000)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--keep-tables", action="store_true")
    ap.add_argument("--out-dir", default="", help="where to write the outputs (default: steps/out)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()

    if a.out_dir:
        s.OUT_DIR = Path(a.out_dir)
    scope = s.load_scope(a.pdf, a.mentions, a.filer)
    fid = scope.cid(scope.filer_id)
    rows, units = trigger_rows(scope, a.window, not a.keep_tables, a.filer_implicit)
    chunks = build_chunks(scope, units, a.window, a.chunk_tokens)
    by_status = collections.Counter(r["status"] or "llm_pending" for r in rows)
    by_type = collections.Counter(r["trigger_type"] for r in rows)
    tok = sum(c.est_tokens(t) for ch in chunks for t in ch["text"])
    print(f"{Path(a.pdf).name}: filer = {scope.filer_name} ({fid}); {len(scope.names)} listed companies; "
          f"{len(rows):,} trigger words in {len({r['paragraph_no'] for r in rows})} paragraphs")
    print("  by type:", dict(by_type))
    print("  by rule:", {k: v for k, v in by_status.items() if k != "llm_pending"})
    print(f"  for the LLM: {by_status['llm_pending']} third-person triggers in {len(units)} paragraphs, "
          f"~{tok:,} input tokens in {len(chunks)} chunks (model {a.model})")
    if a.dry_run:
        return 0

    limiter = c.RateLimiter(a.rpm)
    system = SYSTEM.format(filer=scope.filer_name, fid=fid)
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.monotonic()

    def run(i: int):
        ch = chunks[i]
        res = c.call_llm("\n\n".join(ch["text"]), a.model, max_tokens=a.max_tokens, timeout=a.timeout, limiter=limiter,
                         label=f"chunk {i + 1}", system=system, schema=SCHEMA)
        out, complete = c.parse_references(res.content)
        print(f"  chunk {i + 1}/{len(chunks)}: {len(ch['pids'])} paragraphs -> {len(out)} answers, {res.seconds:.0f}s, "
              f"${res.cost:.4f}{'' if complete and not res.error else '  <-- INCOMPLETE ' + res.error}"
              f"{f'  (429 x{res.rate_limit_hits})' if res.rate_limit_hits else ''}", flush=True)
        return res, out, complete

    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        results = list(pool.map(run, range(len(chunks))))
    wall = time.monotonic() - t0

    answers = {}
    for (res, out, _), ch in zip(results, chunks):
        for r in out:
            answers[(r.get("para"), r.get("trigger_no"))] = (r, ch["allowed"].get(r.get("para"), set()))
    names = {scope.cid(k): v for k, v in scope.names.items()}
    for pid, lst in units.items():
        for r in lst:
            hit = answers.get((pid, r["no"]))
            r["decided_by"] = "llm"
            if not hit:
                r.update(status="no_answer", reason="the model returned no answer for this trigger")
                continue
            ans, allowed = hit
            ref, conf = ans.get("referent_id", ""), ans.get("confidence", 0)
            r["confidence"], r["reason"] = conf, ans.get("reason", "")
            if ref == "NONE" or ref not in allowed:
                r["status"] = "none"
                if ref != "NONE":
                    r["reason"] = f"(answer '{ref}' is not an allowed company) " + r["reason"]
            elif conf < a.min_confidence:
                r.update(status="none", reason=f"(low confidence {conf}) " + r["reason"])
            else:
                r.update(status="resolved", company=names.get(ref, ref), company_id=ref)

    # ---- outputs
    s.OUT_DIR.mkdir(exist_ok=True)
    stem = f"{Path(a.pdf).stem}{a.tag}"
    for i, r in enumerate(rows, 1):
        r["id"] = i
    with open(s.OUT_DIR / f"{stem}_indirect_all_triggers.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=ALL_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    resolved = [r for r in rows if r["status"] == "resolved"]
    main_rows = []
    if a.filer_rows == "paragraph":
        grouped = collections.OrderedDict()
        for r in resolved:
            if r["decided_by"] == "rule":
                grouped.setdefault((r["paragraph_no"], r["company_id"]), []).append(r)
            else:
                main_rows.append({**r, "count": 1})
        for (pid, cid), g in grouped.items():
            words = collections.Counter(x["trigger"].lower() for x in g)
            main_rows.append({**g[0], "trigger": ", ".join(f"{w} x{n}" for w, n in words.most_common()),
                              "count": len(g)})
        main_rows.sort(key=lambda r: (r["paragraph_no"], r["trigger"]))
    else:
        main_rows = [{**r, "count": 1} for r in resolved]
    with open(s.OUT_DIR / f"{stem}_indirect_mentions.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=MAIN_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(main_rows)

    calls = [r for r, _, _ in results]
    failed = [i + 1 for i, (res, _, cm) in enumerate(results) if res.error or not cm]
    st = collections.Counter(r["status"] for r in rows)
    meta = {
        "step": "indirect_mentions", "pdf": Path(a.pdf).name, "model": a.model, "run_started_at": started,
        "mentions_file": a.mentions, "filer": scope.filer_name, "listed_companies": len(scope.names),
        "window": a.window, "filer_implicit": a.filer_implicit, "min_confidence": a.min_confidence, "filer_rows": a.filer_rows,
        "trigger_words": len(rows), "by_type": dict(by_type), "by_status": dict(st),
        "main_csv_rows": len(main_rows), "llm_paragraphs": len(units), "calls": len(chunks),
        "wall_seconds": round(wall, 1), **c.totals(calls),
        "finish_reason": "ok" if not failed else f"chunks incomplete: {failed}", "output_complete": not failed,
        "resolved_to_company": dict(collections.Counter(r["company"] for r in resolved).most_common()),
    }
    (s.OUT_DIR / f"{stem}_indirect_run.json").write_text(
        json.dumps({"meta": meta, "calls": [c.call_record(r, len(o), cm) for r, o, cm in results]}, indent=1,
                   ensure_ascii=False), encoding="utf-8")
    for k, v in meta.items():
        print(f"  {k:24} {v}")
    print(f"saved {s.OUT_DIR / (stem + '_indirect_mentions.csv')}\n      {s.OUT_DIR / (stem + '_indirect_all_triggers.csv')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
