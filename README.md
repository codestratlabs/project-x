# project-x

Cited evidence about private companies, built from public records: SEC filings that mention the company, Form N-PORT, Form D and DOL Form 5500.
The numbers come from code (string match plus table parsing), not from a model. A model is used only in the last step, to find indirect mentions.

## Layout

```
company-finder/                        finds mentions of a company in filings (string match)
  company_finder.py                    command-line entry point
  company_finder/                      package: loader, matchers, extract, output, sec
coref_experiment/
  common.py, approach_d_relations.py   shared library
  coref_verifier/tools/prepare_targeted.py   prepares one company's filings from the mention lists
  steps/indirect_mentions.py           indirect mentions (pronouns near a company mention), LLM step
  steps/run_indirect_all.py            runs indirect_mentions over all prepared filings
  company_report/
    run_company.py                     one command per company (all steps below)
    model_config/<company>.json        everything company-specific (name regex, splits, ...)
    evidence_*.py, validate_all.py, locate.py, final_excels.py, findings_excel.py
    build_report.py, model_*.py          report (kept, not used in the current flow)
    nport.py, build_inputs_*.py        structured sources (N-PORT, Form D, DOL)
```

## Flow

1. **Download and match.** `company-finder` scans the filings and writes `<company>_mentions.csv`.
2. **Prepare.** `prepare_targeted.py <company>` lines up each filing with its mentions.
3. **Evidence, no model.** `run_company.py <company> --cik ... --name ... --evidence` runs:
   `evidence_tables` (reads tables, iXBRL, N-CEN XML, PDF lines) → `evidence_build` (grades rows against N-PORT)
   → `evidence_holdings` → `validate_all` (every number checked against the filing text: printed, unit, date, sums)
   → `evidence_holdings --use-validation` → `evidence_facts` → `evidence_relationships` → `locate` (page and search text)
   → `final_excels` / `findings_excel`.
4. **Structured sources, no model.** N-PORT, Form D and DOL are read as structured records.
5. **Indirect mentions, LLM.** `steps/run_indirect_all.py` (always uses `--filer-implicit off`).

## Setup

```
cd company-finder
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt requests duckdb
cp ../.env.example .env        # fill OPENROUTER_API_KEY and SEC_USER_AGENT (or export them)
```

Data is not in this repo: filings (`dumps/`), the Form D and N-PORT DuckDB files, and all outputs (`out/`).

## Example

```
company-finder/.venv/bin/python coref_experiment/company_report/run_company.py databricks \
  --cik 0001587468 --name "Databricks, Inc." --evidence
```
