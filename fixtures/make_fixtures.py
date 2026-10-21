"""Generate fixture PDFs with *known* ground truth.

Because we author these, every coordinate is known exactly, which makes the
position-error target measurable rather than estimated. Fixtures are always
generated, never real-world documents: a real PDF that shows a bug should be
reduced to an authored fixture that reproduces it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf

# Runnable on its own (python fixtures/make_fixtures.py) as well as from the tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

A4 = (595.276, 841.89)
MARGIN = 56.7
FONT = "helv"
SERIF = "tiro"


def _grid_lines(
    page: pymupdf.Page,
    xs: list[float],
    ys: list[float],
    *,
    skip_h: set[tuple[int, int]] | None = None,
    skip_v: set[tuple[int, int]] | None = None,
) -> None:
    """Draw a ruled table. ``skip_h[(row,col)]`` omits a horizontal segment,
    ``skip_v[(row,col)]`` omits a vertical one -- that is how merged cells are
    represented in a real PDF."""
    skip_h = skip_h or set()
    skip_v = skip_v or set()
    for r in range(len(ys) - 1):
        for c in range(len(xs) - 1):
            if (r, c) not in skip_h:
                page.draw_line((xs[c], ys[r]), (xs[c + 1], ys[r]), color=(0, 0, 0), width=0.7)
            if (r, c) not in skip_v:
                page.draw_line((xs[c], ys[r]), (xs[c], ys[r + 1]), color=(0, 0, 0), width=0.7)
    for c in range(len(xs) - 1):  # bottom edge
        if (len(ys) - 2, c) not in skip_h:
            page.draw_line((xs[c], ys[-1]), (xs[c + 1], ys[-1]), color=(0, 0, 0), width=0.7)
    for r in range(len(ys) - 1):  # right edge
        if (r, len(xs) - 2) not in skip_v:
            page.draw_line((xs[-1], ys[r]), (xs[-1], ys[r + 1]), color=(0, 0, 0), width=0.7)


BOLD_OF = {"helv": "hebo", "tiro": "tibo"}


def _text(page: pymupdf.Page, x: float, baseline: float, s: str, *, size: float = 10, font: str = FONT, bold: bool = False) -> None:
    name = BOLD_OF.get(font, font) if bold else font
    page.insert_text((x, baseline), s, fontname=name, fontsize=size)


def table_basic(path: Path) -> None:
    """Primary fixture: a ruled table with a header row, a rowspan and a colspan."""
    doc = pymupdf.open()
    page = doc.new_page(width=A4[0], height=A4[1])

    _text(page, MARGIN, 80, "Quarterly Sales Report", size=18, bold=True)
    _text(page, MARGIN, 104, "Region: EMEA  |  Period: Q3  |  Currency: EUR", size=9)

    n_cols = 5
    x0, x1 = MARGIN, A4[0] - MARGIN
    step = (x1 - x0) / n_cols
    xs = [x0 + step * i for i in range(n_cols + 1)]

    row_h = 24.0
    top = 150.0
    n_rows = 9  # header + 8
    ys = [top + row_h * i for i in range(n_rows + 1)]

    headers = ["Item", "Qty", "Unit price", "Tax", "Amount"]
    # Rows 1..5 are ordinary. Rows 6..7 share one merged label cell in column 0
    # (a rowspan), and row 8 merges columns 0..1 (a colspan).
    data_rows = [
        (1, ["Widget A", "12", "19.99", "7%", "256.67"]),
        (2, ["Widget B", "4", "149.00", "7%", "637.72"]),
        (3, ["Service plan", "1", "1200.00", "19%", "1428.00"]),
        (4, ["Support hours", "26", "85.00", "19%", "2629.90"]),
        (5, ["Hardware lease", "3", "420.00", "19%", "1499.40"]),
        (6, [None, "1", "95.00", "0%", "95.00"]),
        (7, [None, "1", "30.00", "0%", "30.00"]),
    ]
    merged_label_rows = (6, 7)  # rowspan in column 0
    merged_last_row = 8  # colspan across columns 0..1

    # A vertical line at x=xs[c] is the left edge of column c, so merging columns
    # 0 and 1 means omitting the line at xs[1]. A horizontal line at y=ys[r] is the
    # top edge of row r, so merging rows 6 and 7 means omitting the line at ys[7].
    skip_h = {(merged_label_rows[1], 0)}
    skip_v = {(merged_last_row, 1)}
    _grid_lines(page, xs, ys, skip_h=skip_h, skip_v=skip_v)

    for c, h in enumerate(headers):
        _text(page, xs[c] + 5, ys[0] + 16, h, size=9, bold=True)
    for r, row in data_rows:
        for c, v in enumerate(row):
            if v is not None:
                _text(page, xs[c] + 5, ys[r] + 16, v, size=9)
    # One label, vertically centred in the two-row merged cell.
    _text(page, xs[0] + 5, (ys[merged_label_rows[0]] + ys[merged_label_rows[1] + 1]) / 2 + 3.5,
          "Insurance", size=9)
    _text(page, xs[0] + 5, ys[merged_last_row] + 16, "Total", size=9, bold=True)
    _text(page, xs[4] + 5, ys[merged_last_row] + 16, "6576.69", size=9, bold=True)

    page.draw_line((MARGIN, 130), (A4[0] - MARGIN, 130), color=(0.2, 0.2, 0.2), width=1.2)
    _text(page, MARGIN, 410, "Prepared by the finance team. Figures are unaudited.", size=9, font=SERIF)
    _text(page, A4[0] / 2 - 8, A4[1] - 40, "1", size=9)

    doc.save(path)
    doc.close()


DENSE_ROW_H = 10.2
DENSE_ROWS = 32
DENSE_TOP = 120.0


def table_dense(path: Path) -> None:
    """A ruled table whose rows (10.2pt) are shorter than a browser's default
    12pt line, with small text and a footer just below it. Content-sized rows
    grow past the PDF's and push the table over the footer."""
    doc = pymupdf.open()
    page = doc.new_page(width=A4[1], height=A4[0])  # landscape

    n_cols = 4
    x0, x1 = MARGIN, A4[1] - MARGIN
    step = (x1 - x0) / n_cols
    xs = [x0 + step * i for i in range(n_cols + 1)]
    ys = [DENSE_TOP + DENSE_ROW_H * i for i in range(DENSE_ROWS + 1)]
    _grid_lines(page, xs, ys)

    for r in range(DENSE_ROWS):
        baseline = ys[r] + 7.8
        _text(page, xs[0] + 20, baseline, str(r + 1), size=7.68)
        _text(page, xs[1] + 20, baseline, f"{r + 1:02d}-Jan-26", size=7.68)
        _text(page, xs[2] + 20, baseline, f"{(r + 1) * 1234.5:,.2f}", size=7.68)
        _text(page, xs[3] + 20, baseline, f"{(r + 1) * 98.76:,.2f}", size=7.68)

    _text(page, A4[1] / 2 - 30, ys[-1] + 14, "Page 1 of 1", size=7.68)

    doc.save(path)
    doc.close()


def mixed_text(path: Path) -> None:
    """Text-only fixture: heading levels, a wrapped paragraph, a rule, a footer."""
    doc = pymupdf.open()
    page = doc.new_page(width=A4[0], height=A4[1])

    _text(page, MARGIN, 90, "Reconstruction Notes", size=22, bold=True)
    _text(page, MARGIN, 118, "A short document with headings and wrapped prose.", size=11, font=SERIF)

    para = (
        "The pipeline extracts text with its exact geometry before any inference happens. "
        "Lines are grouped into paragraphs using spacing and font continuity, and each line "
        "is positioned independently so that a semantic element never displaces its content."
    )
    y = 160.0
    for i in range(0, len(para), 78):
        chunk = para[i : i + 78]
        _text(page, MARGIN, y, chunk, size=11, font=SERIF)
        y += 16.5

    _text(page, MARGIN, y + 30, "Second Section", size=15, bold=True)
    y += 62
    for i in range(0, len(para), 78):
        chunk = para[i : i + 78]
        _text(page, MARGIN, y, chunk, size=11, font=SERIF)
        y += 16.5

    page.draw_line((MARGIN, y + 20), (A4[0] - MARGIN, y + 20), color=(0.4, 0.4, 0.4), width=0.8)
    _text(page, A4[0] / 2 - 8, A4[1] - 40, "1", size=9)

    doc.save(path)
    doc.close()


def multipage_report(path: Path) -> None:
    """Three pages, with one table deliberately continuing across a page break."""
    doc = pymupdf.open()
    xs_cols = 5
    x0, x1 = MARGIN, A4[0] - MARGIN
    step = (x1 - x0) / xs_cols
    xs = [x0 + step * i for i in range(xs_cols + 1)]
    headers = ["Ref", "Description", "Qty", "Unit", "Amount"]

    def draw_table(page: pymupdf.Page, top: float, n_rows: int, first: int, last_page: bool) -> float:
        row_h = 26.0
        ys = [top + row_h * i for i in range(n_rows + 1)]
        _grid_lines(page, xs, ys)
        for c, h in enumerate(headers):
            _text(page, xs[c] + 5, ys[0] + 17, h, size=9, bold=True)
        for r in range(1, n_rows + (0 if last_page else 1)):
            if r >= len(ys):
                break
            vals = [f"{first + r:04d}", f"Line item {first + r}", str(r * 3), "ea", f"{r * 137.45:.2f}"]
            for c, v in enumerate(vals):
                _text(page, xs[c] + 5, ys[r] + 17, v, size=9)
        return ys[-1]

    p1 = doc.new_page(width=A4[0], height=A4[1])
    _text(p1, MARGIN, 80, "Annual Line Items", size=18, bold=True)
    _text(p1, MARGIN, 100, "Table continues on the following page.", size=9, font=SERIF)
    draw_table(p1, 400.0, 14, 0, last_page=False)
    _text(p1, A4[0] / 2 - 8, A4[1] - 40, "1", size=9)

    p2 = doc.new_page(width=A4[0], height=A4[1])
    _text(p2, MARGIN, 60, "Annual Line Items (continued)", size=11, bold=True)
    draw_table(p2, 76.0, 18, 14, last_page=False)
    _text(p2, A4[0] / 2 - 8, A4[1] - 40, "2", size=9)

    p3 = doc.new_page(width=A4[0], height=A4[1])
    _text(p3, MARGIN, 90, "Notes", size=18, bold=True)
    y = 130.0
    for i in range(0, 220, 80):
        _text(p3, MARGIN, y, "The reconstruction preserves the original line breaks exactly.", size=11, font=SERIF)
        y += 16.5
    _text(p3, A4[0] / 2 - 8, A4[1] - 40, "3", size=9)

    doc.save(path)
    doc.close()


# --- text converted to outlines ------------------------------------------------
#
# Drawn exactly the way Publisher exports text: no text layer, every letter a
# filled vector outline taken from the font file, one path per run. Some runs
# are squeezed sideways or slanted (synthetic italic), and some marks are drawn
# as their own path, because the real form does all of that. Ground truth goes
# to outlined_text.json next to the PDF.

OUTLINED = [
    # pieces (separate paths, one space apart), (family, bold, italic), size, colour, baseline, stretch, slant
    (["*A1. Nama Penuh"], ("Arial", True, False), 8.04, (0, 0, 0), 100, 1.0, 0.0),
    (["*A5. E-mel"], ("Arial", True, False), 8.04, (0, 0, 0), 120, 1.0, 0.0),
    (["Update (To participate)"], ("Arial", False, True), 7.0, (0, 0, 0), 140, 1.0, 0.0),
    (["MAKLUMAT PEKERJA / PARTICULAR OF EMPLOYEE'S"], ("Arial", True, False), 10.0, (1, 1, 1), 165, 1.0, 0.0),
    (["Kemaskini (Untuk sertai)"], ("Arial Narrow", False, False), 10.0, (0, 0, 0), 190, 1.0, 0.0),
    (["Illegal 1100 OIL in the"], ("Arial", False, False), 9.0, (0, 0, 0), 210, 1.0, 0.0),
    (["Saya memahami bahawa"], ("Arial", True, False), 9.0, (1, 0, 0), 230, 1.0, 0.0),
    (["PERAKUAN PENYERTAAN"], ("Arial", True, False), 9.0, (0, 0, 0), 250, 0.8, 0.0),
    (["Medical Benefit"], ("Arial Narrow", False, False), 8.0, (0, 0, 0), 270, 1.0, 0.21),
    (["MAKLUMAT / INFORMATION", ":"], ("Arial", True, False), 10.0, (0, 0, 0), 290, 1.0, 0.0),
    (["Signature of Employee", "’s"], ("Arial", False, True), 8.0, (0, 0, 0), 310, 1.0, 0.0),
    (["Diperakui Oleh"], ("Calibri", False, False), 9.0, (0, 0, 0), 330, 1.0, 0.0),
]
# Drawn in a family outside the reference set, so it must be left for OCR.
NOT_INSTALLED_FAMILY = ("Hello World", ("Georgia", False, False), 12.0, (0, 0, 0), 360)


def _font_file(face: tuple[str, bool, bool]) -> Path | None:
    """Find an installed font by family and style, on any OS.

    Uses the converter's own lookup: font folders are searched recursively and
    fonts are identified by their name table, so it works whatever the files
    are called (arialbd.ttf on Windows, Arial_Bold.ttf on Ubuntu, "Arial Bold.ttf"
    on macOS)."""
    from pdf2htmldom.glyphs import _family_files, _family_key

    family, bold, italic = face
    return dict(_family_files(_family_key(family))[1]).get((bold, italic))


def _draw_outlined(page: pymupdf.Page, text: str, font_path: Path, size: float, color, x: float,
                   baseline: float, stretch: float = 1.0, slant: float = 0.0) -> float:
    """Draw ``text`` as filled glyph outlines, one path. Returns the pen x after it."""
    from fontTools.pens.basePen import BasePen
    from fontTools.ttLib import TTFont

    font = TTFont(str(font_path))
    gs, cmap, upem = font.getGlyphSet(), font.getBestCmap(), font["head"].unitsPerEm
    s = size / upem
    shape = page.new_shape()
    pen_x = x

    def T(ux, uy):
        return pymupdf.Point(pen_x + s * (ux * stretch + slant * uy), baseline - s * uy)

    class Pen(BasePen):
        def _moveTo(self, p):
            self.start = self.cur = T(*p)

        def _lineTo(self, p):
            q = T(*p)
            shape.draw_line(self.cur, q)
            self.cur = q

        def _qCurveToOne(self, a, p):
            a_, q = T(*a), T(*p)
            c1 = self.cur + (a_ - self.cur) * (2 / 3)
            c2 = q + (a_ - q) * (2 / 3)
            shape.draw_bezier(self.cur, c1, c2, q)
            self.cur = q

        def _curveToOne(self, a, b, p):
            q = T(*p)
            shape.draw_bezier(self.cur, T(*a), T(*b), q)
            self.cur = q

        def _closePath(self):
            if abs(self.cur.x - self.start.x) > 1e-6 or abs(self.cur.y - self.start.y) > 1e-6:
                shape.draw_line(self.cur, self.start)

        _endPath = _closePath

    for ch in text:
        name = cmap.get(ord(ch))
        if name is None:
            continue
        gs[name].draw(Pen(gs))
        pen_x += gs[name].width * s * stretch
    shape.finish(fill=color, color=None, width=0)
    shape.commit()
    font.close()
    return pen_x


def outlined_text(path: Path) -> bool:
    import json

    if _font_file(("Arial", False, False)) is None:
        return False
    doc = pymupdf.open()
    page = doc.new_page(width=A4[0], height=A4[1])
    page.draw_rect(pymupdf.Rect(36, 152, 560, 170), color=None, fill=(0, 0, 1))  # bar behind the white heading
    truth = []
    x0 = 40.0
    for pieces, face, size, color, baseline, stretch, slant in OUTLINED:
        fp = _font_file(face)
        if fp is None:
            continue  # e.g. no Arial Narrow or Calibri on Linux: that line is simply not in the fixture
        x = x0
        text = ""
        for i, piece in enumerate(pieces):
            if i and not piece.startswith("\u2019"):
                x += 0.278 * size * stretch  # a space between separately drawn pieces
                text += " "
            x = _draw_outlined(page, piece, fp, size, color, x, baseline, stretch, slant)
            text += piece
        family, bold, italic = face
        truth.append({
            "text": text,
            "family": family, "size": size, "color": "#%02x%02x%02x" % tuple(round(c * 255) for c in color),
            "baseline": baseline, "bold": bold, "italic": italic or slant != 0, "stretch": stretch,
        })
    text, face, size, color, baseline = NOT_INSTALLED_FAMILY
    fp = _font_file(face)
    if fp is not None:
        _draw_outlined(page, text, fp, size, color, x0, baseline)
    doc.save(path)
    doc.close()
    (path.with_suffix(".json")).write_text(
        json.dumps({"lines": truth, "fallback": text if fp else None}, indent=2), encoding="utf-8"
    )
    return True


def main() -> None:
    here = Path(__file__).parent
    table_basic(here / "table_basic.pdf")
    table_dense(here / "table_dense.pdf")
    mixed_text(here / "mixed_text.pdf")
    multipage_report(here / "multipage.pdf")
    written = ["table_basic.pdf", "table_dense.pdf", "mixed_text.pdf", "multipage.pdf"]
    if outlined_text(here / "outlined_text.pdf"):
        written.append("outlined_text.pdf")
    for f in written:
        print(f"wrote {here / f}")


if __name__ == "__main__":
    main()
