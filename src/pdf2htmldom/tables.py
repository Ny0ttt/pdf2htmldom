"""Stage 2 (table half): deterministic ruled-table detection.

Priority 1 and 2 from the spec -- ruled tables and merged cells -- need no AI and
no custom CV. PyMuPDF's ``find_tables`` already recovers cell geometry from the
PDF's own vector rules, which is both faster and more accurate than anything we
would infer from a rendered bitmap.

Merged cells are handled by de-duplicating cell boxes and measuring each one
against the page's clustered row/column edges, which is robust to the way the
underlying finder reports spans.
"""

from __future__ import annotations

import pymupdf

from .models import BBox, Line, TableBlock, TableCell


def _cluster(values: list[float], tol: float = 3.0) -> list[float]:
    """Group near-equal coordinates into distinct edges, keeping the mean."""
    if not values:
        return []
    ordered = sorted(values)
    groups: list[list[float]] = [[ordered[0]]]
    for v in ordered[1:]:
        if v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(g) / len(g) for g in groups]


def _index_of(edges: list[float], value: float, tol: float = 4.0) -> int:
    best, best_d = 0, float("inf")
    for i, e in enumerate(edges):
        d = abs(e - value)
        if d < best_d:
            best, best_d = i, d
    if best_d > tol:
        raise ValueError(f"coordinate {value} is {best_d:.1f}pt from the nearest edge")
    return best


def _assign_lines(cells: list[TableCell], lines: list[Line]) -> set[str]:
    """Give each line to the *smallest* cell that contains it.

    A merged cell overlaps every grid position it covers, so a naive
    "is it inside?" test lets a large merged cell swallow the text of its smaller
    neighbours. Choosing the smallest containing box resolves that correctly.
    Returns the ids of lines consumed by the table.
    """
    consumed: set[str] = set()
    ordered = sorted(cells, key=lambda c: c.bbox.width * c.bbox.height)
    for ln in lines:
        cx, cy = ln.bbox.cx, ln.bbox.cy
        for cell in ordered:
            b = cell.bbox
            if b.x0 - 1 <= cx <= b.x1 + 1 and b.y0 - 1 <= cy <= b.y1 + 1:
                cell.lines.append(ln)
                consumed.add(ln.id)
                break
    for cell in cells:
        cell.lines.sort(key=lambda ln: (round(ln.baseline, 1), ln.origin_x))
    return consumed


def _plausible(table, page: pymupdf.Page) -> tuple[bool, str]:
    """Reject detections that are geometrically possible but structurally absurd.

    The borderless (``text``) strategy is happy to invent column boundaries in the
    middle of running prose -- it will split "Reconstruction Notes" into
    "Reconstruction Note" + "s" and produce a table with alternating empty rows.
    Blank-row ratio and filled-cell density separate real tables from that.
    """
    rows = table.extract()
    if table.row_count < 2 or table.col_count < 2:
        return False, f"degenerate grid {table.row_count}x{table.col_count}"
    if not rows:
        return False, "no rows"
    blank = sum(1 for r in rows if not any((c or "").strip() for c in r))
    if blank > 0.2 * len(rows):
        return False, f"{blank}/{len(rows)} rows are empty"
    multi = sum(1 for r in rows if sum(1 for c in r if (c or "").strip()) >= 2)
    if multi < 0.5 * len(rows):
        return False, f"only {multi}/{len(rows)} rows have 2+ filled cells"
    x0, y0, x1, y1 = table.bbox
    page_area = page.rect.width * page.rect.height
    if page_area and ((x1 - x0) * (y1 - y0)) / page_area > 0.85:
        return False, "covers almost the whole page"
    return True, "ok"


def detect_tables(
    page: pymupdf.Page,
    lines: list[Line],
    page_no: int,
    strategy: str = "lines",
) -> tuple[list[TableBlock], list[Line], list[str]]:
    """Return (tables, lines_not_consumed_by_a_table, rejection_reasons).

    ``strategy="lines"`` targets ruled tables and is the only default. The
    borderless ``"text"`` strategy is available but must be opted into, because
    per the spec borderless tables are exactly where AI assistance belongs --
    guessing at them deterministically produces confident nonsense.
    """
    try:
        finder = page.find_tables(strategy=strategy)
    except Exception:
        return [], list(lines), []

    tables: list[TableBlock] = []
    rejected: list[str] = []
    consumed: set[str] = set()

    for ti, table in enumerate(finder.tables):
        tbbox = BBox.from_seq(table.bbox)
        if tbbox.width <= 1 or tbbox.height <= 1:
            continue
        ok, why = _plausible(table, page)
        if not ok:
            rejected.append(f"page {page_no}: rejected a {table.row_count}x{table.col_count} table candidate at "
                            f"({tbbox.x0:.0f},{tbbox.y0:.0f}): {why}")
            continue

        # De-duplicate: merged cells are reported once per grid position they cover.
        unique: list[BBox] = []
        for cell in table.cells:
            if cell is None:
                continue
            box = BBox.from_seq(cell)
            if not any(
                abs(box.x0 - u.x0) < 1 and abs(box.y0 - u.y0) < 1 and abs(box.x1 - u.x1) < 1 and abs(box.y1 - u.y1) < 1
                for u in unique
            ):
                unique.append(box)

        xs = _cluster([b.x0 for b in unique] + [b.x1 for b in unique])
        ys = _cluster([b.y0 for b in unique] + [b.y1 for b in unique])
        if len(xs) < 2 or len(ys) < 2:
            continue

        cells: list[TableCell] = []
        for box in unique:
            try:
                c0 = _index_of(xs, box.x0)
                c1 = _index_of(xs, box.x1)
                r0 = _index_of(ys, box.y0)
                r1 = _index_of(ys, box.y1)
            except ValueError:
                continue
            cells.append(
                TableCell(
                    row=r0,
                    col=c0,
                    rowspan=max(1, r1 - r0),
                    colspan=max(1, c1 - c0),
                    bbox=box,
                    is_header=(r0 == 0),
                )
            )

        if not cells:
            continue

        consumed.update(_assign_lines(cells, lines))
        cells.sort(key=lambda c: (c.row, c.col))
        tables.append(
            TableBlock(
                id=f"p{page_no}-t{ti}",
                bbox=tbbox,
                n_rows=max(len(ys) - 1, max(c.row + c.rowspan for c in cells)),
                n_cols=max(len(xs) - 1, max(c.col + c.colspan for c in cells)),
                col_edges=xs,
                row_edges=ys,
                cells=cells,
                strategy=strategy,
            )
        )

    remaining = [ln for ln in lines if ln.id not in consumed]
    return tables, remaining, rejected


def flag_cross_page_tables(tables_by_page: dict[int, list[TableBlock]], page_heights: dict[int, float]) -> list[str]:
    """Priority 4: report tables that appear to continue across a page break.

    Detected, but not yet joined -- the spec asks for this to be *reported*
    rather than silently mis-rendered, and joining changes the IR shape, so it
    is left as an explicit follow-up.
    """
    warnings: list[str] = []
    for pno in sorted(tables_by_page):
        prev = tables_by_page.get(pno - 1, [])
        cur = tables_by_page.get(pno, [])
        if not prev or not cur:
            continue
        h = page_heights.get(pno - 1, 0.0)
        prev_bottom = max(t.bbox.y1 for t in prev)
        cur_top = min(t.bbox.y0 for t in cur)
        if prev_bottom > h - 90 and cur_top < 90:
            warnings.append(
                f"page {pno}: table may continue from page {pno - 1} "
                f"(prev table ends at y={prev_bottom:.0f}, this one starts at y={cur_top:.0f})"
            )
    return warnings
