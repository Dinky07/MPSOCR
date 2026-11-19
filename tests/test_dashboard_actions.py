"""Delete and Move to... from the dashboard (19 Nov: the page sends several at once, without a dialog). What a
delete or a move leaves on disk, and that moves running side by side never pick the same file name."""
import json, tempfile, unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import dashboard as D


class Actions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = {"root": str(self.root), "state_db": str(self.root / "state.db"),
                    "doc_types": {"PurchaseOrders": {"doc_type": "po"}, "SupplierInvoices": {"doc_type": "supplier_invoice"}}}
        for t in self.cfg["doc_types"]:
            (self.root / "out" / t / "hold").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, name, held=False, source="scan.pdf", t="PurchaseOrders"):
        d = self.root / "out" / t / ("hold" if held else "")
        (d / f"{name}.pdf").write_bytes(b"%PDF " + name.encode())
        (d / f"{name}.json").write_text(json.dumps({"doc_type": "po", "file": f"{name}.pdf", "source_file": source,
                                                    "fields": {"order_number": name}}))
        return f"{name}.json"

    def act(self, **body):
        return D.do_action(self.cfg, body)

    def test_delete_keeps_the_pair_for_recovery(self):
        n = self.add("A10001")
        self.assertTrue(self.act(action="delete", type="PurchaseOrders", name=n, held=False, why="MPS_Invoice")["ok"])
        out = self.root / "out" / "PurchaseOrders"
        self.assertEqual([p.name for p in out.iterdir() if p.is_file()], [])            # the lock is gone too
        kept = {p.name for p in (self.root / "deleted" / "PurchaseOrders").rglob("*") if p.is_file()}
        self.assertEqual(kept, {"A10001.pdf", "A10001.json", "A10001.deleted.json"})
        note = json.loads(next((self.root / "deleted").rglob("A10001.deleted.json")).read_text())
        self.assertEqual((note["from"], note["why"]), ("queue", "MPS_Invoice"))

    def test_delete_twice_and_uploaded(self):
        n = self.add("A10002", held=True)
        self.act(action="delete", type="PurchaseOrders", name=n, held=True)
        with self.assertRaises(ValueError):                                              # the second click of a double click
            self.act(action="delete", type="PurchaseOrders", name=n, held=True)
        n = self.add("A10003")
        (self.root / "out" / "PurchaseOrders" / (n + ".uploaded")).write_text("")
        with self.assertRaises(ValueError):
            self.act(action="delete", type="PurchaseOrders", name=n, held=False)
        self.assertTrue((self.root / "out" / "PurchaseOrders" / n).is_file())

    def test_move_to_a_queue(self):
        n = self.add("A10004", source="Scan 7.pdf")
        r = self.act(action="reroute", type="PurchaseOrders", name=n, held=False, dest="SupplierInvoices", label="supplier_invoice")
        self.assertEqual((r["to"], r["name"]), ("SupplierInvoices", "Scan 7.pdf"))
        ind = self.root / "in" / "SupplierInvoices"
        self.assertEqual((ind / "Scan 7.pdf").read_bytes(), b"%PDF A10004")
        self.assertEqual(json.loads((ind / "_meta" / "Scan 7.pdf.json").read_text())["original_name"], "Scan 7.pdf")
        self.assertFalse((self.root / "out" / "PurchaseOrders" / n).exists())
        moves = (self.root / "work" / "sorter" / "moves" / "moves.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(moves[0])["type"], "supplier_invoice")

    def test_moves_side_by_side_take_different_names(self):
        names = [self.add(f"A2{i:04d}", source="same scan.pdf") for i in range(24)]      # 24 documents cut from one scan
        with ThreadPoolExecutor(8) as ex:
            got = list(ex.map(lambda n: self.act(action="reroute", type="PurchaseOrders", name=n, held=False,
                                                 dest="SupplierInvoices")["name"], names))
        self.assertEqual(len(set(got)), 24)
        ind = self.root / "in" / "SupplierInvoices"
        self.assertEqual(len(list(ind.glob("*.pdf"))), 24)
        self.assertEqual(len(list((ind / "_meta").glob("*.json"))), 24)
        self.assertEqual(sorted(p.read_bytes() for p in ind.glob("*.pdf")), sorted(b"%PDF " + n[:-5].encode() for n in names))
        self.assertEqual(len((self.root / "work" / "sorter" / "moves" / "moves.jsonl").read_text().splitlines()), 24)

    def test_deletes_side_by_side(self):
        names = [self.add(f"A3{i:04d}") for i in range(30)]
        with ThreadPoolExecutor(6) as ex:
            list(ex.map(lambda n: self.act(action="delete", type="PurchaseOrders", name=n, held=False), names))
        self.assertEqual([p for p in (self.root / "out" / "PurchaseOrders").iterdir() if p.is_file()], [])
        self.assertEqual(len(list((self.root / "deleted").rglob("*.deleted.json"))), 30)


if __name__ == "__main__":
    unittest.main()
