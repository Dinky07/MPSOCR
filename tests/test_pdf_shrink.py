"""pdf_shrink: big scans get smaller, everything else about the PDF stays as it was."""
import os, random
from io import BytesIO
from pathlib import Path

import pytest

pikepdf = pytest.importorskip("pikepdf")
Image = pytest.importorskip("PIL.Image")
from PIL import ImageDraw  # noqa: E402

import pdf_shrink  # noqa: E402


def _scan_pdf(path, pages=2, dpi=400, rotate=None):
    """A PDF like the Toshiba's: each page one full-page colour JPEG at `dpi`, quality 95, with noise and a red stamp."""
    pdf = pikepdf.new()
    w, h = int(8.27 * dpi), int(11.69 * dpi)
    rnd = random.Random(1)
    for i in range(pages):
        im = Image.new("RGB", (w // 4, h // 4), "white")
        d = ImageDraw.Draw(im)
        for y in range(20, h // 4 - 20, 18):
            d.text((20, y), "INVOICE DW00%04d  qty %d  ZAR %d.%02d" % (i, rnd.randint(1, 99), rnd.randint(1, 9999), y % 100),
                   fill=(rnd.randint(0, 60),) * 3)
        d.rectangle((100, 100, 300, 160), outline=(220, 40, 90), width=4)
        im = im.resize((w, h))
        buf = BytesIO()
        im.save(buf, "JPEG", quality=95)
        img = pikepdf.Stream(pdf, buf.getvalue())
        img.Type, img.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
        img.Width, img.Height, img.ColorSpace, img.BitsPerComponent = w, h, pikepdf.Name.DeviceRGB, 8
        img.Filter = pikepdf.Name.DCTDecode
        page = pikepdf.Dictionary(Type=pikepdf.Name.Page, MediaBox=[0, 0, 595, 842],
                                  Resources=pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=img)),
                                  Contents=pikepdf.Stream(pdf, b"q 595 0 0 842 0 0 cm /Im0 Do Q"))
        pdf.pages.append(pikepdf.Page(page))
    if rotate:
        pdf.pages[-1].rotate(rotate, relative=True)
    pdf.save(str(path))


def test_big_scan_gets_smaller_and_keeps_pages_and_rotation(tmp_path):
    src, dst = tmp_path / "in.pdf", tmp_path / "out.pdf"
    _scan_pdf(src, rotate=90)
    info = pdf_shrink.shrink(src, dst)
    assert info and info["bytes_out"] < 0.85 * info["bytes_in"]
    with pikepdf.open(str(dst)) as out:
        assert len(out.pages) == 2
        assert int(out.pages[1].obj.get("/Rotate", 0)) == 90
        im = next(iter(out.pages[0].images.values()))
        assert abs(int(im.Width) - round(8.27 * 200)) <= 2           # 400 dpi -> 200 dpi
        assert im.ColorSpace == pikepdf.Name.DeviceRGB                 # colour kept (stamps)


def test_already_small_scan_is_left_alone(tmp_path):
    src, dst = tmp_path / "in.pdf", tmp_path / "out.pdf"
    _scan_pdf(src, pages=1, dpi=150)
    pdf_shrink.publish(src, dst, {"quality": 95})
    assert dst.read_bytes() == src.read_bytes()


def test_switched_off_or_not_a_pdf(tmp_path):
    src = tmp_path / "in.pdf"
    _scan_pdf(src, pages=1)
    assert pdf_shrink.shrink(src, tmp_path / "o.pdf", False) is None
    assert pdf_shrink.shrink(tmp_path / "x.png", tmp_path / "o.png") is None
    assert not (tmp_path / "o.pdf").exists()


def _jpeg(path, size, quality):
    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    rnd = random.Random(2)
    for y in range(10, size[1] - 10, 14):
        d.text((10, y), "LINE %d %d" % (y, rnd.randint(0, 99999)), fill=(rnd.randint(0, 80),) * 3)
    im.save(path, "JPEG", quality=quality)


def test_jpeg_quality_estimate(tmp_path):
    for q in (50, 70, 95):
        _jpeg(tmp_path / "q.jpg", (400, 300), q)
        assert abs(pdf_shrink.jpeg_quality((tmp_path / "q.jpg").read_bytes()) - q) <= 2


def test_200dpi_low_quality_scan_left_alone_high_quality_resaved(tmp_path):
    src, dst = tmp_path / "in.pdf", tmp_path / "out.pdf"
    _scan_pdf(src, pages=1, dpi=200)                     # 200 dpi at quality 95: resaved at 70, same size
    info = pdf_shrink.shrink(src, dst)
    assert info
    with pikepdf.open(str(dst)) as out:
        im = next(iter(out.pages[0].images.values()))
        assert abs(int(im.Width) - round(8.27 * 200)) <= 2
        assert abs(pdf_shrink.jpeg_quality(im.read_raw_bytes()) - 70) <= 2
    # that output is now at 200 dpi / quality 70: a second pass leaves it alone
    assert pdf_shrink.shrink(dst, tmp_path / "again.pdf") is None


def test_big_jpg_photo_is_capped(tmp_path):
    src, dst = tmp_path / "photo.jpg", tmp_path / "out.jpg"
    _jpeg(src, (4000, 3000), 95)
    info = pdf_shrink.shrink(src, dst)
    assert info and info["bytes_out"] < info["bytes_in"]
    assert max(Image.open(dst).size) == round(11.69 * 200)
    small = tmp_path / "small.jpg"
    _jpeg(small, (1600, 1200), 60)
    assert pdf_shrink.shrink(small, tmp_path / "o2.jpg") is None
