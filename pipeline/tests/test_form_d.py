"""A Form D primary_doc.xml is read field by field with labels, every value kept (Lungpacer 0001708893-17-000001).

    python pipeline/tests/test_form_d.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "indirect"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import text_llm as c  # noqa: E402

c.MIN_PARA_CHARS = 1   # as the indirect step sets it (scope.py)

XML = """<?xml version="1.0"?><edgarSubmission><schemaVersion>X0707</schemaVersion><submissionType>D</submissionType>
<primaryIssuer><cik>0001708893</cik><entityName>Lungpacer Medical, Inc.</entityName><issuerAddress><street1>8602 COMMERCE COURT</street1>
<city>BURNABY</city></issuerAddress><entityType>Corporation</entityType></primaryIssuer>
<relatedPersonsList><relatedPersonInfo><relatedPersonName><firstName>Michael</firstName><middleName>L.</middleName><lastName>Favet</lastName>
</relatedPersonName><relatedPersonAddress><city>Burnaby</city></relatedPersonAddress><relatedPersonRelationshipList><relationship>Director</relationship>
</relatedPersonRelationshipList><relationshipClarification></relationshipClarification></relatedPersonInfo></relatedPersonsList>
<offeringData><typeOfFiling><newOrAmendment><isAmendment>false</isAmendment></newOrAmendment><dateOfFirstSale><value>2017-05-24</value></dateOfFirstSale></typeOfFiling>
<offeringSalesAmounts><totalOfferingAmount>19624651</totalOfferingAmount><totalAmountSold>19624651</totalAmountSold><totalRemaining>0</totalRemaining>
<clarificationOfResponse>Total is comprised of a cash amount equal to $4,775,000 plus $14,849,650.85 via the conversion of existing convertible notes.</clarificationOfResponse></offeringSalesAmounts>
<useOfProceeds><grossProceedsUsed><dollarAmount>173500</dollarAmount><isEstimate>true</isEstimate></grossProceedsUsed></useOfProceeds>
<signatureBlock><signature><issuerName>Lungpacer Medical, Inc.</issuerName><nameOfSigner>Shannon Kaustinen</nameOfSigner></signature></signatureBlock>
</offeringData></edgarSubmission>"""

with tempfile.TemporaryDirectory() as d:
    f = Path(d) / "primary_doc.xml"
    f.write_text(XML, encoding="utf-8")
    lines = [p.text for p in c.load_paragraphs(str(f))]
    other = Path(d) / "other.xml"
    other.write_text("<root><a>not a Form D</a></root>", encoding="utf-8")
    plain = [p.text for p in c.load_paragraphs(str(other))]
want = ["Entity name: Lungpacer Medical, Inc. (entityName)", "Related person: Michael L. Favet (Director); address: Burnaby",
        "Is amendment: false (isAmendment)", "Date of first sale: 2017-05-24 (dateOfFirstSale/value)", "Total amount sold: 19624651 (totalAmountSold)",
        "Gross proceeds used: 173500 (grossProceedsUsed/dollarAmount)", "Gross proceeds used (estimate): true (grossProceedsUsed/isEstimate)",
        "Signature: Issuer name Lungpacer Medical, Inc.; Name of signer Shannon Kaustinen"]
bad = [w for w in want if w not in lines]
for w in want:
    print("OK " if w in lines else "BAD", w)
print("OK " if not any("X0707" in x for x in lines) else "BAD", "filing envelope left out")
print("OK " if plain == ["not a Form D"] else "BAD", "another XML is read as before:", plain)
assert not bad and not any("X0707" in x for x in lines) and plain == ["not a Form D"]
