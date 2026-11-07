#!/usr/bin/env python3
"""591 (Dino 7 Nov "apply the rules ... from the test certs fork"): the reading rules learnt from 164 certs read by eye
(6-7 Nov, rule_suggestions_2026-11-07.md) added to the test-cert reader's prompt. Tested against the checked test certs
before it goes live (tc_rules_test.py, in the training chain); installed only when no control gets worse.
    tc_rules_patch.py <tc_reader.py>"""
import sys

p = sys.argv[1]
s = open(p, encoding="utf-8").read()
ANCHOR = '''legibility: "good|fair|poor".
Return ONLY this JSON object:'''
RULES = '''Lessons from certificates checked by hand (7 Nov 2026) - these are the mistakes made most often:
- heat_no is ONLY the column headed Heat / Heat No / Cast / Melt / Charge / 炉号. A number headed Lot, Batch, Pack, Bundle,
  Lift card, Plate ID, Product No, Test No, Work No or MFG goes to batch, never to heat_no: Baotou prints a 10-digit Lot
  No beside its 8-digit Heat No; Angang prints pack numbers (S4624297x000); Zhongpu prints a batch (24E057755) beside the
  heat (2422107045); ArcelorMittal plate prints a lift card (VPN47173); Hebei Haihao and Hangzhou Double-Peace print a
  fitting code beside the material heat.
- Row numbers of the chemistry table (1, 2, 3 ... 13) are not heats: give each chemistry row the heat it belongs to.
- qty is the number of PIECES. When a cert gives bundles and pieces (Jiangyin: 10 bundles, 370 pcs), qty is the pieces.
  A pieces column next to the OD ("15 / 20 / 35 / 12") is quantities, not sizes. An empty Quantity box is null, not 1.
- A size column holding a bare inch figure ("6", "16") is inches (6 = 150NB, 16 = 400NB), not millimetres or NB. When the
  schedule is in its own column ("16" and "SCH60"), wt_text is the schedule: "60" there is SCH60, not 60 mm.
- San Eng and other A105 flange mills print the TENSILE strength before the YIELD: read each mechanical value from its
  own column heading (TS / UTS = tensile, YS = yield); the yield is always lower than the tensile.
- cert_no is the certificate number only: ArcelorMittal "Certificate Reference No" (040...), not its Order No (1000...)
  or Material No; Baotou the 10-11 character code, not the record no (CJL/...); Tianjin Huilitong the 质证号 box, not the
  "EN10204/3.1" heading; Evraz P000xxxxxx (P and zeros). A documentary credit / L/C number (IL...ZA...) is never the
  cert_no.
- One page holds one certificate: give the cert_no printed on THIS page. A page with the same cert_no, heat and size as an
  earlier page of the file is a copy, not new items: say so by giving the same cert_no.
- product comes from the items: S/O, WN, BL, SORF, WNRF lines are FLANGES even when the heading names a pipe standard
  (Metalfar lists flanges under A106).
- mill: the letterhead's name, matched to a known mill when it is one of: Shandong Ruiye, San Eng, Iraeta, Hebei Haihao,
  Hebei Xinfeng, Baotou Steel Union, Jiangyin, ArcelorMittal South Africa, Evraz Highveld, Angang, Xinyu, Zhongpu, Hebei
  Huayang, Tianjin Huilitong, Pangang Chengdu (a stamp over the name is not a reason to change it).

'''
if s.count(ANCHOR) != 1:
    sys.exit(f"anchor found {s.count(ANCHOR)} times")
if "Lessons from certificates checked by hand" in s:
    sys.exit("already patched")
s = s.replace(ANCHOR, RULES + ANCHOR)
open(p, "w", encoding="utf-8", newline="\n").write(s)
print("patched", p)
