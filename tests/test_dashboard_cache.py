"""The dashboard's speed-up caches (19 Nov) must never show stale data: a rewritten sidecar, a new file in a folder,
a ticked document or a changed suppliers.csv shows on the next refresh."""
import json, os, sqlite3, tempfile, time, unittest
from pathlib import Path

import dashboard as D


def write(p, obj):
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, p)                      # as every sidecar writer does


class Caches(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "out" / "PurchaseOrders" / "hold").mkdir(parents=True)
        self.cfg = {"root": str(self.root), "state_db": str(self.root / "state.db"),
                    "doc_types": {"PurchaseOrders": {"doc_type": "po"}}}
        D._DOCS.clear(); D._DIRC.clear(); D._SUPP.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_rewritten_sidecar_is_read_again(self):
        p = self.root / "out" / "PurchaseOrders" / "a.json"
        write(p, {"doc_type": "po", "fields": {"order_number": "A10001"}})
        self.assertEqual(D.read_doc(str(p))["fields"]["order_number"], "A10001")
        write(p, {"doc_type": "po", "fields": {"order_number": "A10001", "checked": True}, "checked_at": "x"})
        self.assertTrue(D.read_doc(str(p))["fields"]["checked"])

    def test_out_queue_follows_the_folder(self):
        d = self.root / "out" / "PurchaseOrders"
        write(d / "a.json", {"doc_type": "po", "file": "a.pdf", "fields": {"order_number": "A10001"}})
        row = D.out_queue(self.cfg, "PurchaseOrders")["ready"][0]
        self.assertEqual((row["pdf"], row["uploaded"], row["attempts"]), (False, False, 0))
        (d / "a.pdf").write_bytes(b"%PDF")
        (d / "a.json.uploaded").write_text("")
        (d / "a.json.attempts").write_text("2")
        write(d / "a.json", {"doc_type": "po", "file": "a.pdf", "fields": {"order_number": "A10001", "checked": True}})
        row = D.out_queue(self.cfg, "PurchaseOrders")["ready"][0]
        self.assertEqual((row["pdf"], row["uploaded"], row["attempts"], row["fields"].get("checked")), (True, True, 2, True))

    def test_counts_follow_the_folder(self):
        d = self.root / "out" / "PurchaseOrders"
        self.assertEqual(D.count_files(d, r"\.json$"), 0)
        time.sleep(0.01)
        (d / "b.json").write_text("{}")
        self.assertEqual(D.count_files(d, r"\.json$"), 1)

    def test_suppliers_csv_read_again_when_changed(self):
        p = self.root / "suppliers.csv"
        p.write_text("VatNo,Name,SageAccount\n4000000001,A,ACC1\n")
        self.assertEqual(D.suppliers_vat(self.root)[1], {"4000000001": "ACC1"})
        tmp = self.root / "s.tmp"
        tmp.write_text("VatNo,Name,SageAccount\n4000000002,B,ACC2\n")
        os.replace(tmp, p)
        self.assertEqual(D.suppliers_vat(self.root)[1], {"4000000002": "ACC2"})

    def test_indexes_leave_the_data_alone(self):
        db = sqlite3.connect(self.cfg["state_db"])
        db.execute("CREATE TABLE processed(sha256 TEXT PRIMARY KEY, folder TEXT, source TEXT, status TEXT, outputs TEXT, at TEXT)")
        db.execute("CREATE TABLE extractions(at TEXT, folder TEXT, source TEXT, via TEXT, reasons TEXT)")
        db.execute("INSERT INTO processed VALUES('h','PurchaseOrders','s','ok','[]','2026-11-19T10:00:00')")
        db.commit(); db.close()
        D.ensure_indexes(self.cfg); D.ensure_indexes(self.cfg)
        db = sqlite3.connect(self.cfg["state_db"])
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue({"ix_processed_at", "ix_processed_folder_at", "ix_extractions_at"} <= names)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM processed").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
