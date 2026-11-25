"""Sentence ends that know abbreviations: python pipeline/tests/test_sentences.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evidence"))
import sentences as S
cases=[
 ("Mr. Evans has served as the President and Chief Executive Officer of Lungpacer Medical Inc., a medical device company, since January 2014. Before joining Lungpacer, Mr. Evans served as the Chief Operating Officer.", "Lungpacer Medical", "Mr. Evans has served as the President and Chief Executive Officer of Lungpacer Medical Inc., a medical device company, since January 2014."),
 ("since January 2014. Before joining Lungpacer, Mr. Evans served as the Chief Operating Officer and a member of the board.", "Lungpacer,", "Before joining Lungpacer, Mr. Evans served as the Chief Operating Officer and a member of the board."),
 ("She served as a Director of Othera Pharmaceuticals Inc. Intact Vascular, Inc., and Lungpacer, Inc. Dr. Crothall served as a Director of Animas Corp. since 1996.", "Lungpacer", "She served as a Director of Othera Pharmaceuticals Inc. Intact Vascular, Inc., and Lungpacer, Inc."),
 ("She is a former director of Animas Corporation (NASDAQ:PUMP); Pharmitas Corporation; Intact Vascular, Inc.; Lungpacer, Inc.: Valeritas Holdings, Inc.; and Adhezion BioMedical, Inc. She holds over 20 patents.", "Lungpacer", "She is a former director of Animas Corporation (NASDAQ:PUMP); Pharmitas Corporation; Intact Vascular, Inc.; Lungpacer, Inc.: Valeritas Holdings, Inc.; and Adhezion BioMedical, Inc."),
 ("Director Michael L. Favet 8602 Commerce Court", "Favet", "Director Michael L. Favet 8602 Commerce Court"),
 ("He was an officer of Lungpacer, Inc. The Company paid him $5.", "Lungpacer", "He was an officer of Lungpacer, Inc."),
 ("It operates in the U.S. and Canada. Lungpacer is based in Exton.", "Lungpacer", "Lungpacer is based in Exton."),
 ("It operates in the U.S. We own Lungpacer stock.", "Lungpacer", "We own Lungpacer stock."),
 ("Removed Horizon3 and Quantica. Horizon3 and Quantica were major advisors for Frontier Select Fund.", "Horizon3 and Quantica were", "Horizon3 and Quantica were major advisors for Frontier Select Fund."),
 ("He holds a Ph.D. from Penn. He joined Lungpacer in 2014.", "Lungpacer", "He joined Lungpacer in 2014."),
 ("Mr. Conlin is currently the corporate secretary for Lungpacer Medical Inc. (a venture-backed medical device company headquartered in Exton, PA, USA). He was CFO.", "Lungpacer", "Mr. Conlin is currently the corporate secretary for Lungpacer Medical Inc. (a venture-backed medical device company headquartered in Exton, PA, USA)."),
 ("Holdings: • Lungpacer Medical Series B • Acme Corp common", "Lungpacer", "• Lungpacer Medical Series B"),
 ("The Series A. The Lungpacer note was paid.", "Lungpacer", "The Lungpacer note was paid."),
 ("He said \"we invested in Lungpacer.\" Then he left.", "Lungpacer", "He said \"we invested in Lungpacer.\""),
 ("See No. 5 for Lungpacer details. Next item.", "Lungpacer", "See No. 5 for Lungpacer details."),
]
bad=0
for txt,needle,exp in cases:
  i=txt.index(needle); got=S.sentence_at(txt,i,i+len(needle))
  ok=got==exp; bad+=not ok
  print('OK ' if ok else 'BAD', repr(got) if not ok else exp[:70])
  if not ok: print('    expected',repr(exp))
print('bad',bad)
assert bad == 0
