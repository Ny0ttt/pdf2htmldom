"""Orchestration: PDF -> IR -> HTML -> measured refinement -> verified package."""

from __future__ import annotations

import base64
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import pymupdf

from . import compare as cmp
from .boxes import detect_box_rows, detect_frames
from .browser import BrowserRenderer, compute_corrections
from .ingest import extract_page, open_document
from .models import BBox, Line, Span
from .glyphs import join_pieces, recover_outlined_text
from .ocr import ocr_page, ocr_regions, reconcile

# Up to this many unread outlined regions are OCR'd one by one; beyond it the
# page is mostly unreadable from shapes and is OCR'd whole, as a scan.
MAX_REGION_OCR = 12
from .layout import (
    assign_heading_levels,
    assign_reading_order,
    body_size,
    infer_blocks,
    merge_bar_headings,
    merge_stray_fragments,
)
from .models import Document, Page
from .render import render_clean_document, render_document
from .tables import detect_tables, flag_cross_page_tables

LIGATURES = {
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
    "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st",
}
STRIP_CHARS = "\u00ad\u200b\u200c\u200d\ufeff"


def normalize_text(s: str) -> str:
    """Canonical form for the content-preservation check.

    Ligatures, soft hyphens and zero-width characters are expanded or removed,
    and all whitespace is dropped, because re-flowed HTML cannot be expected to
    reproduce the PDF's exact whitespace runs. What must survive is the
    *characters*.
    """
    s = unicodedata.normalize("NFC", s)
    for k, v in LIGATURES.items():
        s = s.replace(k, v)
    s = "".join(ch for ch in s if ch not in STRIP_CHARS)
    return re.sub(r"\s+", "", s)


@dataclass
class BuildResult:
    out_dir: Path
    pages: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    text_preservation: dict = field(default_factory=dict)
    font_substitutions: dict = field(default_factory=dict)
    refinement: list[dict] = field(default_factory=list)


def ocr_line_to_ir(ol, page_no: int, idx: int) -> Line:
    """Convert an OCR result into an ordinary IR line.

    Everything downstream is unchanged by this: OCR text is just another line
    with a different ``source``.
    """
    x0, y0, x1, y1 = ol.bbox
    span = Span(
        text=ol.text,
        font="OCR-Narrow" if ol.narrow else "OCR",
        size=ol.size,
        color=ol.color,
        bold=ol.bold,
        bbox=BBox(x0=x0, y0=y0, x1=x1, y1=y1),
        origin=(x0, ol.baseline),
        ascender=0.75,
        descender=-0.21,
    )
    return Line(
        id=f"p{page_no}-ocr{idx}",
        bbox=BBox(x0=x0, y0=y0, x1=x1, y1=y1),
        baseline=ol.baseline,
        origin_x=x0,
        spans=[span],
        source="ocr",
    )


def build_ir(
    pdf_path: str,
    page_numbers: list[int] | None = None,
    allow_borderless: bool = False,
    skip_ocr: bool = False,
    ocr_dpi: int = 300,
) -> tuple[Document, pymupdf.Document]:
    """Stages 1, 2 and 4: extraction, table detection, structure inference."""
    doc = open_document(pdf_path)
    wanted = page_numbers or list(range(1, doc.page_count + 1))

    doc_ir = Document(source_file=Path(pdf_path).name, page_count=doc.page_count)
    all_blocks = []
    tables_by_page: dict[int, list] = {}
    heights: dict[int, float] = {}
    raw_pages = []

    for pno in wanted:
        raw = extract_page(doc, pno - 1)
        pymupdf_page = doc[pno - 1]

        # Stage 3a. Text drawn as vector outlines is read back from the letter
        # shapes against the installed fonts -- exact where it matches.
        unread = list(raw.outline_regions)
        if raw.outline_paths:
            g = recover_outlined_text(
                raw.outline_paths,
                raw.number,
                [f[3] for f in pymupdf_page.get_fonts()],
                raw.text_bboxes,
                raw.drawings,
            )
            raw.lines.extend(g.lines)
            raw.text_bboxes.extend((ln.bbox.x0, ln.bbox.y0, ln.bbox.x1, ln.bbox.y1) for ln in g.lines)
            raw.drawings = [d for d in raw.drawings if d.id not in g.dash_drawings]
            unread = g.unread
            if g.lines:
                raw.warnings.append(
                    f"page {raw.number}: {len(g.lines)} line(s) read from outlined letter shapes "
                    f"({', '.join(sorted(g.families))}); {len(unread)} outlined region(s) left to OCR"
                    + (f"; {g.ambiguous} look-alike letter(s) decided by context" if g.ambiguous else "")
                )

        # Stage 3b. OCR fills what is left: outlined regions that did not match
        # (read one by one, so a lone symbol is not lost), or a whole page with
        # almost no text layer (a scan). The PDF's own text always wins.
        real_chars = sum(len(ln.text.strip()) for ln in raw.lines)
        scan_like = real_chars < 40 or len(unread) > MAX_REGION_OCR
        if (scan_like or unread) and not skip_ocr:
            if scan_like:
                ocr_lines, meta = ocr_page(pymupdf_page, dpi=ocr_dpi)
                kept, dropped = reconcile(ocr_lines, raw.text_bboxes)
            else:
                kept, meta = ocr_regions(pymupdf_page, unread)
                dropped = 0
            for i, ol in enumerate(kept):
                raw.lines.append(ocr_line_to_ir(ol, raw.number, i))
            raw.warnings.append(
                f"page {raw.number}: OCR recovered {sum(len(l.text) for l in kept)} chars in "
                f"{len(kept)} lines ({len(unread)} unread outlined region(s), "
                f"{dropped} OCR lines discarded as duplicates of the text layer)"
            )
            if meta.get("error"):
                raw.warnings.append(f"page {raw.number}: OCR failed: {meta['error']}")
        if any(ln.source == "glyph" for ln in raw.lines):
            # Second join pass, now that text-layer fragments can bridge pieces.
            pieces = [ln for ln in raw.lines if ln.source == "glyph"
                      or (ln.source == "pdf" and len(ln.text.strip()) <= 3)]
            ids = {ln.id for ln in pieces}
            joined, _ = join_pieces(pieces, [])
            raw.lines = [ln for ln in raw.lines if ln.id not in ids] + joined
        raw.lines, n_merged = merge_stray_fragments(raw.lines)
        if n_merged:
            raw.warnings.append(
                f"page {raw.number}: {n_merged} text-layer fragment(s) merged into recovered lines"
            )
        raw_pages.append(raw)

        tables, remaining, rejected = detect_tables(pymupdf_page, raw.lines, raw.number)
        warns = list(rejected)
        if not tables and allow_borderless:
            # Opt-in only, and still validated: the text strategy is a guess.
            tables, remaining, rejected2 = detect_tables(
                pymupdf_page, raw.lines, raw.number, strategy="text"
            )
            warns.extend(rejected2)
            if tables:
                warns.append(
                    f"page {raw.number}: borderless table accepted without AI verification "
                    f"({tables[0].n_rows}x{tables[0].n_cols}) -- review required"
                )
        blocks, layout_warns = infer_blocks(remaining, raw.height, raw.width, raw.number)
        warns.extend(layout_warns)

        # A rule inside a detected table is that table's border, not a decorative
        # line. Fold it into the table's CSS borders instead of drawing it twice.
        kept_drawings, folded = _fold_table_rules(raw.drawings, tables)
        if folded:
            warns.append(f"page {raw.number}: {folded} rule(s) folded into table borders")
        box_rows, kept_drawings, absorbed = detect_box_rows(kept_drawings, raw.number)
        if box_rows:
            warns.append(
                f"page {raw.number}: {absorbed} rule(s) rebuilt as {len(box_rows)} form-box row(s) "
                f"({sum(b.count for b in box_rows)} boxes)"
            )
        frames, kept_drawings, framed = detect_frames(kept_drawings, raw.number)
        if frames:
            warns.append(f"page {raw.number}: {framed} rule(s) rebuilt as {len(frames)} frame(s)")
        blocks = merge_bar_headings(blocks, kept_drawings)

        page = Page(
            number=raw.number,
            width=raw.width,
            height=raw.height,
            rotation=raw.rotation,
            warnings=list(raw.warnings) + warns,
        )
        page.elements.extend(tables)
        page.elements.extend(blocks)
        page.elements.extend(kept_drawings)
        page.elements.extend(box_rows)
        page.elements.extend(frames)
        page.elements.extend(raw.images)

        tables_by_page[raw.number] = tables
        heights[raw.number] = raw.height
        all_blocks.extend(blocks)
        doc_ir.pages.append(page)
        doc_ir.fonts_used.extend(sorted(raw.fonts))

    doc_ir.fonts_used = sorted(set(doc_ir.fonts_used))
    body = body_size([ln for raw in raw_pages for ln in raw.lines])
    assign_heading_levels(all_blocks, body)
    for page in doc_ir.pages:
        assign_reading_order([e for e in page.elements if hasattr(e, "reading_order")])

    doc_ir.warnings.extend(flag_cross_page_tables(tables_by_page, heights))
    if doc_ir.page_count > len(wanted):
        doc_ir.warnings.append(f"only pages {wanted} reconstructed ({doc.page_count} in file)")

    # Assets
    return doc_ir, doc


def _fold_table_rules(drawings: list, tables: list) -> tuple[list, int]:
    """Drop vector rules that lie inside a table, since the table now owns them.

    The measured stroke width is carried onto the table so its CSS borders are
    as heavy as the original's rules -- a fixed width loses real ink and shows up
    directly as reduced recall.
    """
    kept = []
    folded = 0
    for t in tables:
        widths = [
            d.width for d in drawings if t.bbox.contains(d.bbox, tol=1.0) and d.width and d.width > 0
        ]
        if widths:
            t.rule_width = float(np.median(widths))
    for d in drawings:
        if any(t.bbox.contains(d.bbox, tol=1.0) for t in tables):
            folded += 1
        else:
            kept.append(d)
    return kept, folded


def _write_assets(doc: pymupdf.Document, out_dir: Path, pages: list[int] | None = None) -> None:
    assets = out_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for pno in pages or range(1, doc.page_count + 1):
        raw = extract_page(doc, pno - 1)
        for name, (data, _ext) in raw.image_bytes.items():
            (assets / name).write_bytes(data)


def dom_text(renderer: BrowserRenderer) -> str:
    """All rendered text, each character counted exactly once.

    It must be ``body.textContent`` and not a walk over ``[data-el]``: those
    elements nest (a ``<td>`` contains line spans), so walking them would count
    every table cell twice and report phantom content.
    """
    return renderer._page.evaluate("() => document.body.textContent")


def check_text_preservation(pdf_text: str, html_text: str) -> dict:
    """Every character of the PDF must appear in the DOM.

    This is a *multiset* comparison, not a positional diff, and that is
    deliberate: PDF text order is content-stream order, which is not reading
    order. A table's merged-cell label can be drawn after every other cell, so a
    sequence diff reports missing text that is present and correctly placed.

    Reading order is asserted by construction (DOM order == inferred reading
    order) and validated visually by the ink and line-position metrics, which do
    not care how the PDF happened to serialise its content.
    """
    a, b = normalize_text(pdf_text), normalize_text(html_text)
    ca, cb = Counter(a), Counter(b)
    matched = sum(min(ca[ch], cb[ch]) for ch in ca)
    missing = ca - cb
    extra = cb - ca
    return {
        "pdf_chars": len(a),
        "html_chars": len(b),
        "matched_chars": matched,
        "preserved_pct": round(100.0 * matched / len(a), 3) if a else 100.0,
        "missing_samples": [ch * n for ch, n in list(missing.items())[:10]],
        "extra_samples": [ch * n for ch, n in list(extra.items())[:10]],
        "note": "multiset comparison; PDF content order is not reading order",
    }


def build(
    pdf_path: str,
    out_dir: str | Path,
    *,
    device_scale: float = 1.5,
    max_iters: int = 3,
    page_numbers: list[int] | None = None,
    single_file: bool = False,
    allow_borderless: bool = False,
    skip_ocr: bool = False,
    ocr_dpi: int = 300,
    fit_ocr_size: bool = False,
) -> BuildResult:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    result = BuildResult(out_dir=out)

    doc_ir, doc = build_ir(
        pdf_path,
        page_numbers,
        allow_borderless=allow_borderless,
        skip_ocr=skip_ocr,
        ocr_dpi=ocr_dpi,
    )
    result.warnings.extend(doc_ir.warnings)
    _write_assets(doc, out, page_numbers)

    # The browser measures a detailed render whose data-* attributes carry each
    # line's target geometry. Users get the clean render written further down;
    # both place every element at the same coordinates.
    measure_html = out / "_measure.html"
    html, css, info = render_document(doc_ir)
    measure_html.write_text(_inline_css(html, css), encoding="utf-8")
    (out / "document.json").write_text(doc_ir.model_dump_json(indent=2), encoding="utf-8")
    result.font_substitutions = info.font_substitutions

    matrix_scale = device_scale * 4 / 3  # CSS px -> PDF points
    cumulative: dict[str, dict] = {}

    with BrowserRenderer(device_scale=device_scale) as br:
        br.load(measure_html)

        # --- Stage 9: measured refinement -----------------------------------
        # Two independent phases, because they interfere if run together:
        # changing a font size moves the baseline, so a loop that fixes both at
        # once chases its own tail. Width is linear in size and position is a
        # pure translation, so each phase converges on its own in 1-2 passes.
        phases = [("size", ("fs",)), ("position", ("dy", "ls"))] if fit_ocr_size else [("position", ("dy", "ls"))]
        for phase, levers in phases:
            previous_score = float("inf")
            for it in range(max_iters):
                measurements = br.measure()
                corrections = compute_corrections(measurements, levers=levers)
                before = _error_stats(measurements)
                if not corrections:
                    result.refinement.append(
                        {"phase": phase, "iter": it, "applied": 0, "before": before, "after": before}
                    )
                    break
                for el_id, rec in corrections.items():
                    acc = cumulative.setdefault(el_id, {})
                    for k, v in rec.items():
                        # Baseline and tracking deltas accumulate; a size fit is a
                        # multiplier, so successive passes compose rather than add.
                        acc[k] = acc.get(k, 1.0) * float(v) if k == "fs" else acc.get(k, 0.0) + float(v)
                br.apply_corrections(corrections)
                remeasured = br.measure()
                after = _error_stats(remeasured)
                score = _residual_score(remeasured)
                result.refinement.append(
                    {"phase": phase, "iter": it, "applied": len(corrections),
                     "before": before, "after": after, "residual": round(score, 4)}
                )
                # Stop when the pass made nothing better. Without this the loop
                # keeps re-issuing tracking corrections that oscillate, and the
                # accumulated drift is worse than the error it was chasing.
                if score >= previous_score - 1e-4:
                    break
                previous_score = score
            # Width changed, so re-measure before the position phase starts.
            br.measure()

        # Final artifact is re-rendered from the IR + corrections, so it is
        # correct as static HTML with no JavaScript involved.
        html_final, css_final, _info2 = render_document(doc_ir, corrections=cumulative)
        measure_html.write_text(_inline_css(html_final, css_final), encoding="utf-8")
        br.load(measure_html)
        final_measurements = br.measure()

        # The deliverable. Validation below (text, screenshots, SSIM, ink) runs
        # on this file, so the scores describe what the user actually gets.
        clean_html, clean_css = render_clean_document(doc_ir, corrections=cumulative)
        (out / "index.html").write_text(clean_html, encoding="utf-8")
        (out / "styles.css").write_text(clean_css, encoding="utf-8")
        br.load(out / "index.html")
        html_text = dom_text(br)

        # --- Stage 8: validation --------------------------------------------
        for page in doc_ir.pages:
            ref_pix = doc[page.number - 1].get_pixmap(
                matrix=pymupdf.Matrix(matrix_scale, matrix_scale), alpha=False
            )
            # PyMuPDF hands back RGB; OpenCV works in BGR. Without this the
            # reference has red and blue swapped, which corrupts the grayscale
            # conversion that feeds SSIM and the ink masks -- and writes a
            # misleading "original" PNG for the user.
            ref = cv2.cvtColor(
                np.frombuffer(ref_pix.samples, np.uint8).reshape(ref_pix.height, ref_pix.width, 3),
                cv2.COLOR_RGB2BGR,
            )

            gen_png = out / f"page-{page.number}-generated.png"
            br.screenshot_element(f"#page-{page.number}", gen_png)
            gen = cv2.imread(str(gen_png), cv2.IMREAD_COLOR)

            comparison = cmp.compare(ref, gen, final_measurements)
            cv2.imwrite(str(out / f"page-{page.number}-diff.png"), cmp.diff_image(ref, gen))
            cv2.imwrite(str(out / f"page-{page.number}-original.png"), ref)
            _side_by_side(ref, gen, out / f"page-{page.number}-side-by-side.png")

            payload = comparison.summary()
            payload.update(
                {
                    "page": page.number,
                    "size_pt": [round(page.width, 2), round(page.height, 2)],
                    "ref_px": [int(ref.shape[1]), int(ref.shape[0])],
                    "gen_px": [int(gen.shape[1]), int(gen.shape[0])],
                    "worst_lines": comparison.line_errors[:5],
                    "width_issue_details": comparison.width_issues[:10],
                    "render_warnings": page.warnings,
                }
            )
            result.pages.append(payload)

    measure_html.unlink(missing_ok=True)
    pdf_text = "".join(doc[p - 1].get_text() for p in (page_numbers or range(1, doc.page_count + 1)))
    result.text_preservation = check_text_preservation(pdf_text, html_text)

    if single_file:
        _write_single_file(out, doc_ir)

    report = {
        "source": doc_ir.source_file,
        "pages": result.pages,
        "text_preservation": result.text_preservation,
        "font_substitutions": result.font_substitutions,
        "refinement": result.refinement,
        # result.warnings already includes doc_ir.warnings; do not add them twice.
        "warnings": result.warnings,
        "acceptance": _acceptance(result),
    }
    (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return result


def _residual_score(measurements: dict) -> float:
    """Total remaining error, used to decide whether a pass helped.

    Combines baseline error and width error so that a pass which fixes one at the
    expense of the other does not count as progress.
    """
    total = 0.0
    for m in measurements.values():
        if "target_baseline" not in m:
            continue
        total += abs(m["baseline"] - m["target_baseline"])
        if m.get("target_w", 0) > 0:
            total += abs(m["w"] - m["target_w"])
    return total


def _error_stats(measurements: dict) -> dict:
    errs = [
        abs(m["baseline"] - m["target_baseline"])
        for m in measurements.values()
        if "target_baseline" in m
    ]
    if not errs:
        return {"mean_pt": 0.0, "max_pt": 0.0, "n": 0}
    arr = np.array(errs)
    return {"mean_pt": round(float(arr.mean()), 4), "max_pt": round(float(arr.max()), 4), "n": len(errs)}


def _side_by_side(ref: np.ndarray, gen: np.ndarray, path: Path, gap: int = 24) -> None:
    h = max(ref.shape[0], gen.shape[0])
    w = ref.shape[1] + gen.shape[1] + gap
    canvas = np.full((h, w, 3), 235, np.uint8)
    canvas[: ref.shape[0], : ref.shape[1]] = ref
    x = ref.shape[1] + gap
    canvas[: gen.shape[0], x : x + gen.shape[1]] = gen
    cv2.imwrite(str(path), canvas)


def _inline_css(html: str, css: str) -> str:
    return html.replace('<link rel="stylesheet" href="styles.css">', f"<style>\n{css}\n</style>")


def _write_single_file(out: Path, doc_ir: Document) -> None:
    html = (out / "index.html").read_text(encoding="utf-8")
    css = (out / "styles.css").read_text(encoding="utf-8")
    html = html.replace('<link rel="stylesheet" href="styles.css">', f"<style>\n{css}\n</style>")
    assets = out / "assets"
    if assets.is_dir():
        for f in assets.iterdir():
            if not f.is_file():
                continue
            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(
                f.suffix.lstrip(".").lower(), "application/octet-stream"
            )
            b64 = base64.b64encode(f.read_bytes()).decode()
            html = html.replace(f'src="assets/{f.name}"', f'src="data:{mime};base64,{b64}"')
    (out / "standalone.html").write_text(html, encoding="utf-8")


def _acceptance(result: BuildResult) -> dict:
    """Report honestly against the targets agreed in the spec."""
    if not result.pages:
        return {}
    mean_err = float(np.mean([p["line_error_mean_pt"] for p in result.pages]))
    max_err = float(np.max([p["line_error_max_pt"] for p in result.pages]))
    ssim_min = float(np.min([p["ssim"] for p in result.pages]))
    recall_min = float(np.min([p["ink_recall"] for p in result.pages]))
    preserved = result.text_preservation.get("preserved_pct", 0.0)
    return {
        "text_preserved_pct": {"value": preserved, "target": 100.0, "pass": preserved >= 99.99},
        "line_error_mean_pt": {"value": round(mean_err, 3), "target": 1.5, "pass": mean_err <= 1.5},
        "line_error_max_pt": {"value": round(max_err, 3), "target": 4.0, "pass": max_err <= 4.0},
        "ssim_min": {"value": round(ssim_min, 4), "target": 0.92, "pass": ssim_min >= 0.92},
        "ink_recall_min": {"value": round(recall_min, 4), "target": 0.98, "pass": recall_min >= 0.98},
        "width_issues": {
            "value": sum(p["width_issues"] for p in result.pages),
            "target": 0,
            "pass": sum(p["width_issues"] for p in result.pages) == 0,
        },
    }
