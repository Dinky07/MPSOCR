"""regression.py end to end on a made-up pipeline root, with a fake Ollama.  python3 -m unittest discover -s tests -t .   (from the pipeline folder)"""
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.fake_ollama import FakeOllama   # noqa: E402

try:
    import regression, training            # noqa: E401,E402
    from PIL import Image                  # noqa: E402
    SKIP = shutil.which("pdftoppm") is None
except Exception:                          # pragma: no cover
    SKIP = True


def hashes_in_test_split(n):        # not named test_*: pytest took it for a test and failed on its argument
    """Content hashes that land in the TEST split (training.is_test)."""
    out, i = [], 0
    while len(out) < n:
        h = hashlib.sha256(str(i).encode()).hexdigest().upper()
        if training.is_test(h):
            out.append(h)
        i += 1
    return out


@unittest.skipIf(SKIP, "needs pdftoppm and Pillow")
class RegressionEndToEnd(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        hs = hashes_in_test_split(4)
        po = {"order_number": "A12345", "supplier": "Acme Steel", "date": "2026-11-01", "checked": True,
              "items": [{"description": "PIPE 100NB", "quantity": 2, "unit_price": 10}]}
        dn = {"dn_number": "IN123", "date": "2026-11-02", "checked": True}
        self.add("PurchaseOrders", "po", hs[0], po, dict(po, supplier="Acme Stee1"))
        self.add("PurchaseOrders", "po", hs[1], dict(po, order_number="A99999"), None)
        self.add("DeliveryNotes", "delivery_note", hs[2], dn, None)
        self.add("PurchaseOrders", "po", hs[3], dict(po, checked=False), None)         # not checked: never in the set
        self.cfg = {"root": str(self.root), "ollama_url": "http://127.0.0.1:9", "doc_types": {}}
        (self.root / "training").mkdir()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def add(self, folder, doc_type, h, fields, read):
        d = self.root / "out" / folder
        d.mkdir(parents=True, exist_ok=True)
        name = h[:8]
        Image.new("RGB", (200, 280), "white").save(d / f"{name}.pdf")
        sc = {"doc_type": doc_type, "folder": folder, "file": f"{name}.pdf", "source_sha256": h,
              "fields": dict(fields, content_hash=h)}
        if read:
            sc["fields_read"] = dict(read, content_hash=h)
        (d / f"{name}.json").write_text(json.dumps(sc))

    def quiet(self, fn, *a):
        with redirect_stdout(io.StringIO()):
            return fn(*a)

    def test_build_baseline_run_compare(self):
        self.quiet(regression.cmd_build, self.cfg, 30, None)
        cs = regression.cases(self.cfg)
        self.assertEqual(sorted(c["kind"] for c in cs), ["delivery_note", "po", "po"])
        self.quiet(regression.cmd_build, self.cfg, 30, None)                       # again: nothing added twice
        self.assertEqual(len(regression.cases(self.cfg)), 3)

        base = json.loads(Path(self.quiet(regression.cmd_baseline, self.cfg, None)).read_text())
        self.assertEqual(base["types"]["po"]["fields"]["supplier"], {"right": 1, "counted": 2, "acc": 0.5})
        self.assertEqual(base["types"]["delivery_note"]["overall"], 1.0)

        answer = {"order_number": "A12345", "supplier": "Acme Steel", "date": "2026-11-01",
                  "line_items": [{"description": "PIPE 100NB", "quantity": 2, "unit_price": 10}]}
        with FakeOllama({"message": {"content": json.dumps(answer)}, "done_reason": "stop"}) as o:
            self.cfg["ollama_url"] = o.url
            p = self.quiet(regression.cmd_run, self.cfg, None, "test", True)
            self.assertTrue(all(r[1] == "/api/chat" for r in o.requests))
            self.assertEqual(len(o.requests), 2)
        self.assertFalse((self.root / "PAUSE").exists())                          # the worker is resumed
        run = json.loads(Path(p).read_text())
        self.assertEqual(set(run["types"]), {"po"})                               # delivery notes: baseline only
        self.assertEqual(run["types"]["po"]["failed_reads"], 0)

        bad = dict(run)
        bad["types"] = {"po": dict(run["types"]["po"], failed_reads=2)}
        bp = self.root / "bad.json"
        bp.write_text(json.dumps(bad))
        self.assertEqual(self.quiet(regression.cmd_compare, p, p, 0.02, 1), 0)
        self.assertEqual(self.quiet(regression.cmd_compare, p, str(bp), 0.02, 1), 1)


if __name__ == "__main__":
    unittest.main()
