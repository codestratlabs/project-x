# project-x

Cited evidence about private companies, built from public records: SEC filings that mention the company, Form N-PORT,
Form D and DOL Form 5500. Every number comes from code (string match, table rules, structured records), not from a model.
A language model is used in one place only: to decide who "it", "its", "they" or "the company" refers to (indirect mentions).

The output for a company is one workbook, `<Name>_findings.xlsx`: All evidence, Indirect mentions, Indirect test,
About, Holdings (filings), N-PORT holdings, Form D offerings, DOL 401(k). Every row has the value as written in the
source and a link that opens the filing at that text.

## Layout

```
tools/                          getting the data (SEC client: User-Agent, 5 requests/s, retries)
  download_filings.py           EDGAR full-text search on the config's names -> dumps/<company>/ + manifest
  build_formd_db.py             the company's own Form D notices -> input/formd/formd.duckdb
  make_companies_file.py        pipeline/config/<company>.json -> company-finder's companies file
  sec_http.py                   shared SEC access
  build_pack.py                 one command from downloaded filings to <Name>_findings.xlsx + <Name>_report.docx
  report_docx.py                the Word report (layout of the Databricks report), counted from the pipeline's outputs
company-finder/                 finds mentions of a company in filings (string match; optional look-alike check)
pipeline/
  paths.py                      every folder the pipeline reads or writes (environment variables, see below)
  config/<company>.json         everything company-specific (name regex, aliases, CIK, split, 401(k) plan ...)
  run_company.py                one command per company: records -> evidence -> prepare -> indirect -> workbook
  sources/nport.py              Form N-PORT: list, download, parse, build nport_holdings.csv
  sources/records.py            N-PORT price marks, DOL Form 5500 rows, Form D offerings
  evidence/                     rule-based evidence (no model)
    evidence_tables.py          reads every filing: table rows, iXBRL tags, N-CEN XML, PDF lines, sentences that name the company
    second_reader.py            second table reader for rows the first one holds back for their layout
    evidence_build.py           grades rows (N-PORT, printed unit), sets aside look-alike filings (finder_verdicts.py)
    evidence_holdings.py        one row per position
    validate_all.py             every number checked against the filing text: printed, unit, date, sums
    evidence_facts.py           dated numbers stated in sentences
    locate.py                   printed page and search text of every row
  prepare_filings.py            each filing with its company-finder mentions (input of the indirect step)
  indirect/                     indirect mentions (language model)
    run_all.py                  runs indirect_mentions.py over all prepared filings of a company
    indirect_mentions.py        trigger words -> rules -> model (GPT-4.1) -> code check -> second model
    scope.py, mentions_map.py, text_llm.py   paragraphs, company mentions per paragraph, model calls
    html_reader/                pinned copy of company-finder's HTML reader, so the paragraphs the indirect step
                                works on (and its page numbers) do not change when company-finder changes
    locate_mentions.py          printed page and highlight link of each indirect mention
  pages.py                      printed page numbers (shared by locate.py and locate_mentions.py)
  findings_excel.py             the findings workbook
```

## Folders

Data is never in this repo. Folders are set in `pipeline/paths.py` and can be moved with environment variables:

| Variable | Default | Holds |
|---|---|---|
| `PX_DATA` | the folder above this checkout | `dumps/` and `input/formd/formd.duckdb` |
| `PX_DUMPS` | `$PX_DATA/dumps` | downloaded filings and their manifests, `<company>_nport/nport_holdings.csv` |
| `PX_FINDER_ROOT` | `$PX_DATA` | company-finder runs: `out/<folder>*/` (or `<run>/out/<folder>_*/`) and `input/<company>_input.csv` |
| `PX_WORK` | `<repo>/work` | everything the pipeline builds: `prepared/`, `inputs/<company>/data/`, `out/<company>/` |

## Flow for one company

1. **Config.** Write `pipeline/config/<company>.json` (copy an existing one: name, aliases, `name_regex`, CIK).
2. **Download.**
   `tools/download_filings.py <company>` (and `<company>_ncsr` with `--term` for fund reports),
   `tools/build_formd_db.py <company>`,
   `pipeline/sources/nport.py list|download|build --dump $PX_DUMPS/<company>_nport --out $PX_DUMPS/<company>_nport/nport_holdings.csv`.
3. **String match.** `tools/make_companies_file.py <company>`, then company-finder on each dump folder, writing
   `$PX_FINDER_ROOT/out/<folder>/<folder>_mentions.csv` (with `--llm-disambiguate` it also drops look-alike names and
   lists them in `<folder>_dropped.csv`).
4. **Everything else.** `pipeline/run_company.py <company> --cik ... --name "..." --dest <folder> [--indirect]`:
   records -> evidence (no model) -> prepare -> indirect (only with `--indirect`, costs money) -> workbook.
5. **Report.** `tools/report_docx.py <company> "<Name>" <findings.xlsx> <out.docx> --template Databricks_report.docx`.

`tools/build_pack.py` runs steps 3 to 5 in one command for filings that are already downloaded (see below).

## Setup

Python 3.10 or newer (macOS's built-in `python3` is 3.9; use e.g. `python3.11`).

```
cd company-finder
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp ../.env.example .env        # fill OPENROUTER_API_KEY and SEC_USER_AGENT (or export them)
```

## Example

```
company-finder/.venv/bin/python pipeline/run_company.py databricks \
  --cik 0001587468 --name "Databricks, Inc." --dest ../Databricks_Verification --indirect
```

Steps can be run on their own, e.g. the indirect step for one filing without a model call:

```
company-finder/.venv/bin/python pipeline/indirect/indirect_mentions.py <filing> --mentions <filing>_mentions.csv \
  --filer "<filer>" --filer-implicit off --dry-run
```

## One command for a pack (filings already downloaded)

For a company whose filings are already in a folder (one subfolder per form, plus a manifest CSV, the layout of
`tools/download_filings.py`), `tools/build_pack.py` runs string match (exact names and aliases, one row per company per
paragraph, look-alike names dropped by the model), N-PORT from the team database, the structured records, the evidence,
the indirect mentions and the workbook, then writes the Word report. The source folders are only read: they are linked
into `work/dumps/`, and company-finder writes to `work/finder/`.

```
company-finder/.venv/bin/python tools/build_pack.py kalshi --cik 0001806928 --name "Kalshi Inc." \
  --filings <data>/sec_filings/kalshi --manifest <data>/sec_filings/kalshi_manifest.csv \
  --nport-db <data>/nport_team.duckdb --formd-db <data>/formd.duckdb --employee-dir <data>/employee_docs/raw \
  --template <data>/Databricks_report.docx --dest <output>/Kalshi --hand-checked 3
```

Output: `<dest>/Kalshi_findings.xlsx` and `<dest>/Kalshi_report.docx`. `--finder-input <file>` uses an existing
company-finder companies file instead of one built from the config; `--no-indirect` and `--no-llm` skip the model steps;
`--hand-checked N` puts the result of reading the indirect words by hand into the report. Configs used so far:
`kalshi.json`, `arize_ai.json` (model cost of a run: about $0.23 and $0.02).

Known gaps (not fixed yet): the DOL 401(k) sheet reads only the full Form 5500, so a company that files the short
Form 5500-SF (Kalshi) shows no rows there (the report lists the 5500-SF numbers); an N-CEN fund-transaction row is
labelled "Verified: same row as one in Holdings" even when Holdings is empty (the report explains it).
