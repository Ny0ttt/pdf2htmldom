"""Stage 1: PDF ingestion. Deterministic extraction, no AI.

Everything downstream trusts these coordinates, so this module is deliberately
boring: it wraps PyMuPDF and normalises its output into the IR primitives.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import pymupdf

from .models import BBox, DrawingBlock, ImageBlock, Line, Span

# PyMuPDF span flag bits
F_SUPERSCRIPT = 1
F_ITALIC = 2
F_SERIF = 4
F_MONO = 8
F_BOLD = 16


def _hexcolor(value: int) -> str:
    """Packed sRGB int, as used by text spans."""
    return f"#{value & 0xFFFFFF:06x}"


def _rgb_hex(value) -> str | None:
    """Drawing colours come back as float triples in 0..1, not packed ints."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return _hexcolor(int(value))
    parts = [max(0, min(255, int(round(float(c) * 255)))) for c in value[:3]]
    return "#%02x%02x%02x" % tuple(parts)


def items_to_svg_path(items: list) -> str:
    """Convert PyMuPDF drawing items to an SVG path ``d`` attribute.

    Coordinates are emitted in PDF points, and the containing ``<svg>`` uses a
    matching ``viewBox``, so no rescaling is involved.
    """
    parts: list[str] = []
    prev_end = None
    for it in items:
        kind = it[0]
        if kind == "re":
            r = it[1]
            parts.append(f"M{r.x0:.3f} {r.y0:.3f}H{r.x1:.3f}V{r.y1:.3f}H{r.x0:.3f}Z")
            prev_end = None
            continue
        if kind == "qu":
            q = it[1]
            parts.append(
                f"M{q.ul.x:.3f} {q.ul.y:.3f}L{q.ur.x:.3f} {q.ur.y:.3f}"
                f"L{q.lr.x:.3f} {q.lr.y:.3f}L{q.ll.x:.3f} {q.ll.y:.3f}Z"
            )
            prev_end = None
            continue
        p0, pend = it[1], it[-1]
        if prev_end is None or abs(p0.x - prev_end.x) > 0.05 or abs(p0.y - prev_end.y) > 0.05:
            parts.append(f"M{p0.x:.3f} {p0.y:.3f}")
        if kind == "l":
            parts.append(f"L{pend.x:.3f} {pend.y:.3f}")
        elif kind == "c":
            parts.append(
                f"C{it[2].x:.3f} {it[2].y:.3f} {it[3].x:.3f} {it[3].y:.3f} {pend.x:.3f} {pend.y:.3f}"
            )
        prev_end = pend
    return "".join(parts)


def count_contours(items: list) -> int:
    """Number of separate subpaths in one drawing path.

    This is the discriminator between real graphics and text that has been
    converted to outlines. A form box, a checkbox or a rule is one continuous
    contour; a word drawn as outlines is one contour per glyph, so it lands in
    the tens. Rendering those as a filled box paints a solid bar over the page.
    """
    starts, prev_end = 0, None
    for it in items:
        if it[0] == "re":
            starts += 1
            prev_end = None
            continue
        p0, pend = it[1], it[-1]
        if prev_end is None or abs(p0.x - prev_end.x) > 0.05 or abs(p0.y - prev_end.y) > 0.05:
            starts += 1
        prev_end = pend
    return starts


@dataclass
class RawPage:
    """Extracted page content, before any structural inference."""

    number: int
    width: float
    height: float
    rotation: int
    lines: list[Line] = field(default_factory=list)
    drawings: list[DrawingBlock] = field(default_factory=list)
    images: list[ImageBlock] = field(default_factory=list)
    fonts: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    image_bytes: dict[str, tuple[bytes, str]] = field(default_factory=dict)  # asset -> (bytes, ext)
    outline_regions: list[tuple[float, float, float, float]] = field(default_factory=list)
    outline_paths: list[dict] = field(default_factory=list)  # PyMuPDF drawing dicts, for glyph matching
    text_bboxes: list[tuple[float, float, float, float]] = field(default_factory=list)


def _spans_from_line(raw_line: dict, page_no: int, line_no: int) -> list[Span]:
    spans: list[Span] = []
    for s in raw_line.get("spans", []):
        text = s.get("text", "")
        if not text:
            continue
        flags = s.get("flags", 0)
        spans.append(
            Span(
                text=text,
                font=s.get("font", "unknown"),
                size=float(s.get("size", 0.0)),
                color=_hexcolor(s.get("color", 0)),
                bold=bool(flags & F_BOLD),
                italic=bool(flags & F_ITALIC),
                serif=bool(flags & F_SERIF),
                mono=bool(flags & F_MONO),
                bbox=BBox.from_seq(s["bbox"]),
                origin=(float(s["origin"][0]), float(s["origin"][1])),
                ascender=float(s.get("ascender", 0.8)),
                descender=float(s.get("descender", -0.2)),
            )
        )
    return spans


def _apply_soft_mask(image: bytes, mask: bytes) -> bytes:
    """Merge an image with its /SMask into one PNG with an alpha channel."""
    import io

    from PIL import Image

    base = Image.open(io.BytesIO(image)).convert("RGB")
    alpha = Image.open(io.BytesIO(mask)).convert("L")
    if alpha.size != base.size:
        alpha = alpha.resize(base.size)
    base.putalpha(alpha)
    buf = io.BytesIO()
    base.save(buf, format="PNG")
    return buf.getvalue()


def extract_page(doc: pymupdf.Document, page_no: int, asset_prefix: str = "p") -> RawPage:
    """Extract one page into raw primitives. ``page_no`` is 0-based."""
    page = doc[page_no]
    # page.rect already accounts for /Rotate, so downstream geometry is in the
    # visual (rotated) coordinate system.
    rect = page.rect
    prefix = asset_prefix
    raw = RawPage(
        number=page_no + 1,
        width=float(rect.width),
        height=float(rect.height),
        rotation=int(page.rotation),
    )
    if page.rotation:
        raw.warnings.append(
            f"page {raw.number}: /Rotate={page.rotation} normalised to "
            f"{rect.width:.0f}x{rect.height:.0f}pt"
        )

    # --- text ---------------------------------------------------------------
    page_dict = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_LIGATURES)
    for block in page_dict.get("blocks", []):
        btype = block.get("type")
        if btype == 0:
            for li, raw_line in enumerate(block.get("lines", [])):
                spans = _spans_from_line(raw_line, raw.number, li)
                # A line whose text is only whitespace carries no content and its
                # bbox is unreliable; keeping it pollutes every metric.
                if not spans or not "".join(sp.text for sp in spans).strip():
                    continue
                direction = raw_line.get("dir", (1.0, 0.0))
                if abs(direction[1]) > 1e-3:
                    raw.warnings.append(
                        f"page {raw.number}: rotated text line skipped "
                        f"(dir={direction[0]:.3f},{direction[1]:.3f})"
                    )
                    continue
                baseline = min(s.origin[1] for s in spans)
                if raw_line["bbox"][2] > raw_line["bbox"][0]:
                    raw.text_bboxes.append(tuple(raw_line["bbox"]))
                raw.lines.append(
                    Line(
                        id=f"{asset_prefix}{raw.number}-l{len(raw.lines)}",
                        bbox=BBox.from_seq(raw_line["bbox"]),
                        baseline=baseline,
                        origin_x=min(s.origin[0] for s in spans),
                        spans=spans,
                    )
                )
                for s in spans:
                    raw.fonts.add(s.font)
        elif btype == 1:
            # --- image ------------------------------------------------------
            ext = block.get("ext", "png")
            data = block.get("image")
            if not data:
                continue
            if block.get("mask"):
                # The soft mask is the image's transparency. Without it a logo on
                # a transparent background renders on solid black.
                data, ext = _apply_soft_mask(data, block["mask"]), "png"
            digest = hashlib.sha256(data).hexdigest()[:12]
            asset = f"{asset_prefix}{raw.number}-img-{digest}.{ext}"
            raw.image_bytes[asset] = (data, ext)
            raw.images.append(
                ImageBlock(
                    id=f"{asset_prefix}{raw.number}-i{len(raw.images)}",
                    bbox=BBox.from_seq(block["bbox"]),
                    asset=asset,
                    width_px=int(block.get("width", 0)),
                    height_px=int(block.get("height", 0)),
                )
            )

    # --- vector drawings ----------------------------------------------------
    for i, d in enumerate(page.get_drawings()):
        rect_d = d.get("rect")
        if rect_d is None:
            continue
        items = d.get("items", [])
        is_rect = bool(items) and all(it[0] == "re" for it in items)
        # A path's bounding box is only a faithful stand-in when the path *is* a
        # rectangle. Anything else must be drawn as its real geometry or not at
        # all -- painting the box turns a word into a solid black bar.
        # A rule drawn as a line is a rule, not a glyph: it is thin in exactly one
        # dimension. Excluding those makes separators and table borders vanish.
        thin = (rect_d.height <= 1.6 and rect_d.width > 8) or (
            rect_d.width <= 1.6 and rect_d.height > 8
        )
        text_shaped = (not is_rect) and (not thin) and rect_d.height <= 30
        if text_shaped:
            # Text converted to outlines. Keep the path so the letters can be read
            # back from their shapes (OCR is the fallback), and never paint it.
            raw.outline_regions.append((rect_d.x0, rect_d.y0, rect_d.x1, rect_d.y1))
            raw.outline_paths.append(d)
            continue
        raw.drawings.append(
            DrawingBlock(
                id=f"{asset_prefix}{raw.number}-d{i}",
                bbox=BBox(x0=rect_d.x0, y0=rect_d.y0, x1=rect_d.x1, y1=rect_d.y1),
                fill=_rgb_hex(d.get("fill")),
                stroke=_rgb_hex(d.get("color")) if d.get("color") is not None else None,
                width=float(d.get("width") or 0.0),
                n_items=len(items),
                is_rect=is_rect,
                path_d=None if is_rect else items_to_svg_path(items),
                even_odd=bool(d.get("even_odd")),
            )
        )

    return raw


def open_document(path: str) -> pymupdf.Document:
    """Open and sanity-check a PDF. Untrusted input: keep it defensive."""
    doc = pymupdf.open(path)
    if doc.is_encrypted and not doc.authenticate(""):
        raise ValueError("PDF is password-protected")
    if doc.page_count == 0:
        raise ValueError("PDF has no pages")
    return doc
