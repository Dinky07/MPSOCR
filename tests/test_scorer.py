"""The shared scorer gives the same numbers as each training script's own scorer.  python3 -m unittest discover -s tests -t .   (from the pipeline folder)"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scorer                       # noqa: E402

try:                                 # the training scripts import the readers; skip if their tools are missing here
    import training, ws_training, jt_training, tc_training      # noqa: E401,E402
except Exception as ex:              # pragma: no cover
    training = None
    WHY = str(ex)

R = random.Random(7)
TEXT = [None, "", "A12345", "a-12345", "A12346", "Joanne", "JOANNE ", "2026-10-09", "9.10.2026", "09/10/2026", "[illegible]",
        "PL 77", "x", 0, 12, "12.0"]
NUMS = [None, "", 1, 1.0, "1", 2, 2.005, "2.00", "abc", 3.14159]


def pick(xs):
    return R.choice(xs)


def maybe(d, k, v):
    if R.random() < 0.85:
        d[k] = v


def doc(fields, items_key, line_keys, bools=()):
    d = {}
    for k in fields:
        maybe(d, k, pick(TEXT))
    for k in bools:
        maybe(d, k, pick([True, False, None, 0, 1, "yes"]))
    if R.random() < 0.8:
        lines = []
        for _ in range(R.randint(0, 4)):
            li = {}
            for k, how in line_keys:
                if how == "dims":
                    maybe(li, k, {"a": pick(TEXT), "b": pick(TEXT)} if R.random() < 0.7 else None)
                else:
                    maybe(li, k, pick(NUMS if how == "num" else TEXT))
            lines.append(li)
        d[items_key] = lines
    return d


@unittest.skipIf(training is None, "training scripts not importable")
class SameAsTraining(unittest.TestCase):
    N = 3000

    def check(self, kind, ref_compare, gen):
        for _ in range(self.N):
            read, truth = gen(), gen()
            self.assertEqual(scorer.compare(read, truth, kind), ref_compare(read, truth), (read, truth))
            self.assertEqual(scorer.compare(truth, truth, kind), ref_compare(truth, truth))

    def test_po(self):
        self.check("po", training.compare, lambda: doc(training.FIELDS, "items",
                   [("description", "text"), ("quantity", "num"), ("unit_price", "num")]))

    def test_ws(self):
        plain = [k for k in ws_training.FIELDS if k not in ws_training.BOOLS]
        self.check("ws", ws_training.compare, lambda: doc(plain, "items",
                   [("item_no", "text"), ("qty", "num"), ("size", "text"), ("dwg_no", "text"), ("dims", "dims")],
                   bools=ws_training.BOOLS))

    def test_jt(self):
        self.check("jt", jt_training.compare, lambda: doc(jt_training.FIELDS, "line_items",
                   [("quantity", "text"), ("description", "text")]))

    def test_tc(self):
        def gen():
            return doc(tc_training.FIELDS, "items", [("size_nb", "num"), ("qty", "num"), ("heat_no", "text"),
                                                     ("heat_no_read", "text")])
        for _ in range(self.N):
            read, truth = gen(), gen()
            read["heats"] = [{"heat_no": pick(TEXT)} for _ in range(R.randint(0, 3))]
            gold = [scorer.norm_text(pick(TEXT)) for _ in range(R.randint(0, 3))]
            gold = [g for g in gold if g]
            checked = R.random() < 0.7
            e = {"gold_heats": gold, "checked": checked, "fields": truth}
            self.assertEqual(scorer.compare(read, truth if checked else None, "tc", gold_heats=gold),
                             tc_training.compare(read, e), (read, e))

    def test_score_totals(self):
        pairs = [(doc(training.FIELDS, "items", [("description", "text")]), doc(training.FIELDS, "items", [("description", "text")]))
                 for _ in range(200)]
        self.assertEqual(scorer.score(pairs, "po"), training.score(pairs))


class Gate(unittest.TestCase):
    def test_regressions(self):
        old = {"overall": 0.9, "fields": {"date": {"acc": 0.95, "counted": 40}, "supplier": {"acc": 0.8, "counted": 40},
                                          "rare": {"acc": 1.0, "counted": 2}}}
        new = {"overall": 0.89, "fields": {"date": {"acc": 0.90, "counted": 40}, "supplier": {"acc": 0.81, "counted": 40},
                                           "rare": {"acc": 0.0, "counted": 2}}}
        self.assertEqual(scorer.regressions(old, new), [("date", 0.95, 0.90)])
        self.assertEqual(scorer.regressions(old, old), [])

    def test_generic(self):
        truth = {"dn_number": "IN123", "date": "2026-11-01", "checked": True, "content_hash": "AB", "items": [{}, {}]}
        c = scorer.compare({"dn_number": "in-123", "date": "2026-11-02", "items": [{}]}, truth, "dn")
        self.assertEqual(c, {"dn_number": (1, 1), "date": (0, 1), "lines_count": (0, 1)})


if __name__ == "__main__":
    unittest.main()
