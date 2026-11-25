"""A year is not an amount: a board table row ('Director since 2020') is not a position, a row with a real amount and no
header is still held back (TELA Bio DEF 14A 0001104659-23-046172 and 0001104659-20-050364).

    python pipeline/tests/test_tables.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evidence"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import evidence_tables as E  # noqa: E402

FAILS = []


def problem(cells):
    t = "<table><tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr></table>"
    m = re.search("lungpacer", t, re.I)
    r = E.from_table(t, m, "lungpacer", [[]])
    return r and r.get("problem")


def check(name, got, want):
    print("OK " if got == want else "BAD", name, "" if got == want else f"got {got!r} want {want!r}")
    if got != want:
        FAILS.append(name)


NOT = "not a position: no amount in the row"
check("row 39: board table, year 2020", problem(["Doug Evans", "58", "2020", "President and Chief Executive Officer of Lungpacer Medical Inc.", "Yes", "M", "M"]), NOT)
check("row 12: board table, year printed as a dash", problem(["Doug Evans", "56", "—", "Chief Executive Officer of Lungpacer Medical Inc.", "Yes", "M", "M"]), NOT)
check("amount with a comma: held back", problem(["Lungpacer Medical Inc. Series B", "1,250,000", "2,020"]), "no column header found")
check("year-like amount with $: held back", problem(["Lungpacer Medical Inc. note", "$2020"]), "no column header found")
check("amount 2,020 alone: held back", problem(["Lungpacer Medical Inc. common", "2,020"]), "no column header found")
check("bare 4-digit non-year 4500: held back", problem(["Lungpacer Medical Inc. common", "4500"]), "no column header found")
check("amount_cell: 2020", E.amount_cell("2020"), False)
check("amount_cell: 1999", E.amount_cell("1999"), False)
check("amount_cell: 2,020", E.amount_cell("2,020"), True)
check("amount_cell: 2020.5", E.amount_cell("2020.5"), True)
check("amount_cell: 99", E.amount_cell("99"), False)
print("failures:", len(FAILS))
assert not FAILS
