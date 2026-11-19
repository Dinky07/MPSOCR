"""Supplier-invoice scans holding several invoices are split per invoice (inv_split, 19 Nov 2026).
python3 -m unittest discover -s tests -t .   (from the pipeline folder)"""
import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import inv_split                      # noqa: E402


def inv(no, page=None, of=None, supplier="Hall Longmore Infrastructure (Pty) Ltd", heading="Tax Invoice : "):
    pg = f"\n                         Page {page} of {of}" if page else ""
    return (f"{supplier}\nVAT Reg No. 4050278647\n{heading}{no}\nCustomer : MIN001 MINING PRESSURE SYSTEMS\n"
            f"Customer Vat : 4320123039\n500 X 300 X 13.5 SANS BAR   1.000  EA  25,278.48\n"
            f"Total Amount  ZAR  25,278.48\nTotal Vat @ 15.00%  ZAR  3,791.77\nTotal net amount  ZAR  29,070.25{pg}")


TERMS = "Terms and conditions of sale\n1. Ownership passes on payment.\n2. Interest at prime plus 2%."
DELNOTE = "DELIVERY NOTE  No. DN88812\nPage 1 of 1\nReceived in good order"


class Groups(unittest.TestCase):
    def g(self, texts):
        return inv_split.groups(texts)[0]

    def test_three_one_page_invoices(self):            # the Hall Longmore file Dino sent, 19 Nov
        self.assertEqual(self.g([inv("DW002836", 1, 1), inv("DW002843", 1, 1), inv("DW002844", 1, 1)]), [[1], [2], [3]])

    def test_one_invoice_over_two_pages_with_number_on_each(self):
        self.assertEqual(self.g([inv("IN12345", 1, 2), inv("IN12345", 2, 2)]), [[1, 2]])

    def test_continuation_page_without_number(self):
        self.assertEqual(self.g([inv("IN12345", 1, 2), "Page 2 of 2\nline 14 ... 1,000.00"]), [[1, 2]])

    def test_terms_and_delivery_note_stay_with_their_invoice(self):
        self.assertEqual(self.g([inv("IN1", 1, 1), TERMS, DELNOTE, inv("IN2", 1, 1), TERMS]), [[1, 2, 3], [4, 5]])

    def test_ocr_misread_of_the_same_number_is_not_a_new_invoice(self):
        self.assertEqual(self.g([inv("INV10O3"), inv("INV1003")]), [[1, 2]])

    def test_page_two_of_two_never_starts_an_invoice(self):     # e.g. a digit misread on the second page
        self.assertEqual(self.g([inv("88123", 1, 2), inv("88128", 2, 2)]), [[1, 2]])

    def test_no_numbers_but_page_1_markers(self):
        self.assertEqual(self.g([inv("", 1, 1, heading="TAX INVOICE"), inv("", 1, 1, heading="TAX INVOICE")]), [[1], [2]])

    def test_invoice_no_label(self):
        self.assertEqual(self.g([inv("A-5512", heading="Invoice No: "), inv("A-5513", heading="Invoice No: ")]), [[1], [2]])

    def test_single_page(self):
        self.assertEqual(self.g([inv("X1")]), [[1]])

    def test_nothing_readable(self):
        self.assertEqual(self.g(["", "", ""]), [[1, 2, 3]])


@unittest.skipUnless(shutil.which("pdfunite") and shutil.which("pdfinfo"), "poppler not installed")
class Worker(unittest.TestCase):
    """worker.split() on a 3-page file: three parts, their page texts kept (no second OCR), the split cached."""

    def test_split_and_note(self):
        import worker
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            pdf = td / "scan.pdf"
            _blank_pdf(pdf, 3)
            w = worker.Worker.__new__(worker.Worker)
            w.cfg, w.root = {}, td
            w.cpu_cache, w.part_text, w.part_info = {}, {}, {}
            texts = [(inv("DW002836", 1, 1), "tesseract"), (inv("DW002843", 1, 1), "tesseract"), (inv("DW002844", 1, 1), "tesseract")]
            orig = inv_split.page_texts
            inv_split.page_texts = lambda *a, **k: texts
            try:
                tcfg = {"doc_type": "supplier_invoice", "split_invoices": True, "max_pages": 4}
                parts = w.split(pdf, td, tcfg, "abc123", "SupplierInvoices", "scan.pdf")
                self.assertEqual([p for p, _ in parts], [1, 2, 3])
                for (_, part), (t, _) in zip(parts, texts):
                    self.assertEqual(w.part_text[str(part)][0], t)
                    out = subprocess.run(["pdfinfo", str(part)], capture_output=True, text=True).stdout
                    self.assertRegex(out, r"Pages:\s+1\n")
                self.assertEqual(w.part_info[str(parts[1][1])], {"pages": [2], "of": 3, "invoices_in_file": 3})
                # the CPU lane's note keeps every part and the split; the GPU worker reads the split back without OCR
                w.cpu_cache["abc123:1"] = ({"invoice_number": "DW002836"}, [], "t1", "tesseract")
                w.cpu_cache["abc123:2"] = ({"invoice_number": None}, ["no_total"], "t2", "tesseract")
                w.write_cpu_note("SupplierInvoices", "scan.pdf", "abc123")
                note = json.loads(w.cpu_note("SupplierInvoices", "scan.pdf").read_text())
                self.assertEqual(sorted(note["parts"]), ["abc123:1", "abc123:2"])
                self.assertEqual(note["split"]["groups"], [[1], [2], [3]])
                inv_split.page_texts = lambda *a, **k: self.fail("pages read again")
                w2 = worker.Worker.__new__(worker.Worker)
                w2.cfg, w2.root, w2.cpu_cache, w2.part_text, w2.part_info = {}, td, {}, {}, {}
                (td / "p2").mkdir()
                self.assertEqual(len(w2.split(pdf, td / "p2", tcfg, "abc123", "SupplierInvoices", "scan.pdf")), 3)
            finally:
                inv_split.page_texts = orig

    def test_one_invoice_file_is_left_whole(self):
        import worker
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            pdf = td / "scan.pdf"
            _blank_pdf(pdf, 2)
            w = worker.Worker.__new__(worker.Worker)
            w.cfg, w.root, w.cpu_cache, w.part_text, w.part_info = {}, td, {}, {}, {}
            orig = inv_split.page_texts
            inv_split.page_texts = lambda *a, **k: [(inv("IN1", 1, 2), "text_layer"), (inv("IN1", 2, 2), "text_layer")]
            try:
                parts = w.split(pdf, td, {"doc_type": "supplier_invoice", "split_invoices": True}, "d", "SupplierInvoices", "scan.pdf")
                self.assertEqual(parts, [(None, pdf)])
                self.assertEqual(w.part_text[str(pdf)][1], "text_layer")
            finally:
                inv_split.page_texts = orig


def _blank_pdf(path, n):
    """An n-page PDF without any library: one empty page object per page."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join(f"{3 + i} 0 R" for i in range(n)), n)]
    objs += ["<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] >>"] * n
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    x = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode()
    Path(path).write_bytes(out)


if __name__ == "__main__":
    unittest.main()
