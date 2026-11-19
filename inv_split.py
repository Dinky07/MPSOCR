#!/usr/bin/env python3
"""inv_split: one scanned supplier-invoice file that holds several invoices -> one part per invoice (Dino 19 Nov 2026).

Dino, 19 Nov: "there is a file here with a few invoices but it is not split and only the first page is captured"
(Hall Longmore DW002836.pdf: three one-page tax invoices DW002836, DW002843, DW002844 in one scan). The Supplier
Invoices queue reads a file as one invoice (split_pages false: most multi-page invoices are ONE invoice), so the
reader took page 1 and the other invoices were lost inside the PDF.

groups(pages) decides where each invoice starts from each page's text (text layer, else Tesseract). Page i starts a
new invoice when it is not a continuation page ("Page 2 of 3") and
  - it carries an invoice number that is not the current invoice's (O/0, I/1, S/5, B/8 misreads count as the same), or
  - it says "Page 1 of N" and is headed as an invoice (Tax Invoice / Invoice) while the current invoice already has
    its own "Page 1 of" page.
Anything else (a second page with no number, terms and conditions, a delivery note or statement stapled behind)
stays with the invoice before it. A file with one invoice is left exactly as it was.

    python3 inv_split.py <pdf> [--write DIR]      shows the pages, their invoice numbers and the split
"""
import re, subprocess, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cpu_invoice  # noqa: E402

PAGE_OF = re.compile(r"(?i)\bpage\s*[:.]?\s*(\d{1,3})\s*(?:of|/)\s*(\d{1,3})\b")
HEADING = re.compile(r"(?i)\b(?:tax\s+)?invoice\b")
NOT_INVOICE = re.compile(r"(?i)\b(?:credit\s+note|statement\s+of\s+account|delivery\s+note|remittance|quotation|pro\s*-?\s*forma)\b")
# "Tax Invoice : DW002836", "TAX INVOICE  No. 12345", "Invoice # INV-0042" (the label cpu_invoice.L_INVNO wants a "No")
INV_HEAD = re.compile(r"(?i)\b(?:tax\s+)?invoice\s*(?:no\.?|nr\.?|number|num|#)?\s*[:.#]\s*([A-Z]{0,4}[\-/]?\d[A-Z0-9/\-]{2,20})")
CONFUSE = str.maketrans("OIlSBZQ", "0115820")


def run(cmd, timeout=180):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def npages(pdf):
    return int(re.search(r"Pages:\s+(\d+)", run(["pdfinfo", str(pdf)], 60)).group(1))


def page_texts(pdf, dpi=300, lang="eng"):
    """[(text, source)] for every page of the PDF: its text layer when the page has one, else Tesseract."""
    out = []
    with tempfile.TemporaryDirectory() as td:
        for i in range(1, npages(pdf) + 1):
            one = Path(td) / f"p{i:03d}.pdf"
            run(["pdfseparate", "-f", str(i), "-l", str(i), str(pdf), str(one)], 60)
            out.append(cpu_invoice.get_text(one, 1, dpi, lang))
    return out


def norm(no):
    return re.sub(r"[^A-Z0-9]", "", str(no or "").upper()).translate(CONFUSE).lstrip("0")


def invoice_no(text):
    """The invoice number printed on one page, or None."""
    for ln in (text or "").splitlines():
        for rx in (INV_HEAD, cpu_invoice.L_INVNO):
            m = rx.search(ln)
            if m and re.search(r"\d", m.group(1)) and len(re.sub(r"\D", "", m.group(1))) >= 3:
                return m.group(1).upper().strip("-/_.")
    n, _ = cpu_invoice.invoice_number_column(text)
    return n


def page_info(text):
    m = PAGE_OF.search(text or "")
    pg = (int(m.group(1)), int(m.group(2))) if m and 0 < int(m.group(1)) <= int(m.group(2)) else None
    return {"invoice_number": invoice_no(text), "page_of": pg,
            "invoice_heading": bool(HEADING.search(text or "")) and not NOT_INVOICE.search((text or "")[:600])}


def groups(texts):
    """texts: one text per page. Returns (groups, infos): groups is a list of 1-based page lists, in order."""
    infos = [page_info(t) for t in texts]
    out, cur_no, cur_has_p1 = [], None, False
    for i, inf in enumerate(infos, 1):
        pg, no = inf["page_of"], inf["invoice_number"]
        cont = bool(pg) and pg[0] > 1                              # "Page 2 of 3": always the invoice before
        start = False
        if out and not cont:
            if no and cur_no and norm(no) != norm(cur_no):
                start = True
            elif pg and pg[0] == 1 and inf["invoice_heading"] and cur_has_p1 and (not no or not cur_no or norm(no) != norm(cur_no)):
                start = True
        if not out or start:
            out.append([i])
            cur_no, cur_has_p1 = no, bool(pg and pg[0] == 1)
        else:
            out[-1].append(i)
            cur_no = cur_no or no
            cur_has_p1 = cur_has_p1 or bool(pg and pg[0] == 1)
        inf["starts"] = len(out[-1]) == 1
    return out, infos


def write_parts(pdf, grps, td):
    """One PDF per group in td: [(first page, path)]."""
    td = Path(td)
    parts = []
    for g in grps:
        pages = []
        for p in g:
            one = td / f"inv-page-{p:03d}.pdf"
            if not one.exists():
                run(["pdfseparate", "-f", str(p), "-l", str(p), str(pdf), str(one)], 60)
            pages.append(str(one))
        dst = td / (f"invoice-p{g[0]:03d}-{g[-1]:03d}.pdf")
        if len(pages) == 1:
            dst.write_bytes(Path(pages[0]).read_bytes())
        else:
            run(["pdfunite", *pages, str(dst)], 120)
        parts.append((g[0], dst))
    return parts


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    pdf = Path(sys.argv[1])
    texts = page_texts(pdf)
    grps, infos = groups([t for t, _ in texts])
    for i, (inf, (_, src)) in enumerate(zip(infos, texts), 1):
        print(f"page {i:>3}: {'NEW ' if inf['starts'] else '    '}invoice no {inf['invoice_number'] or '-':<14} "
              f"page_of {inf['page_of'] or '-'}  heading {inf['invoice_heading']}  ({src})")
    print(f"{len(grps)} invoice(s): {grps}")
    if "--write" in sys.argv[2:]:
        d = Path(sys.argv[sys.argv.index("--write") + 1])
        d.mkdir(parents=True, exist_ok=True)
        for first, p in write_parts(pdf, grps, d):
            print("wrote", p)


if __name__ == "__main__":
    main()
