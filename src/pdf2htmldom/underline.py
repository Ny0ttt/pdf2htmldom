"""Underlines: a PDF has no underline style, only a thin rule drawn under the text.

Each such rule is turned back into a property of the text it sits under, and the
drawing is dropped so the line is not painted twice. A rule counts as an
underline only when it is thin, starts just under a line's baseline, has the
text's colour and lies inside that line's horizontal extent -- a separator or a
table border runs wider than the text above it, and a strikethrough sits above
the baseline, so neither is taken.

A rule under part of a run underlines only the words it covers. Words are
located by their advance widths in the matching base-14 face, scaled to the
run's measured width.
"""

from __future__ import annotations

import re

import pymupdf

from .models import BBox, DrawingBlock, Line, Span

MAX_THICKNESS = 0.2  # of the font size
MAX_DROP = 0.35  # how far under the baseline the rule may start, as a fraction of the font size
WORD_COVERAGE = 0.6  # share of a word's width the rule must cover


def _rule_geometry(d: DrawingBlock) -> tuple[float, float, float, float, str] | None:
    """(x0, x1, top, thickness, colour) of a thin horizontal rule, else None."""
    bb = d.bbox
    if d.fill and d.is_rect:
        thickness, color = bb.height, d.fill
    elif d.stroke and bb.height < 0.5:  # a stroked line: the stroke is the thickness
        thickness, color = max(bb.height, d.width), d.stroke
    else:
        return None
    if bb.width < 2 or thickness <= 0:
        return None
    return bb.x0, bb.x1, bb.cy - thickness / 2, thickness, color.lower()


def _face(s: Span) -> str:
    if s.mono:
        family = ("cour", "cobo", "coit", "cobi")
    elif s.serif or "times" in s.font.lower():
        family = ("tiro", "tibo", "tiit", "tibi")
    else:
        family = ("helv", "hebo", "heit", "hebi")
    return family[(1 if s.bold else 0) + (2 if s.italic else 0)]


def _word_ranges(s: Span) -> list[tuple[str, float, float]]:
    """Split a run into words and spaces with an estimated x-range for each."""
    tokens = re.findall(r"\S+|\s+", s.text)
    face = _face(s)
    widths = [pymupdf.get_text_length(t, fontname=face, fontsize=s.size) for t in tokens]
    total = sum(widths) or 1.0
    scale = s.bbox.width / total
    out, x = [], s.bbox.x0
    for t, w in zip(tokens, widths):
        out.append((t, x, x + w * scale))
        x += w * scale
    return out


def _covered(x0: float, x1: float, a: float, b: float) -> bool:
    return b > a and (min(x1, b) - max(x0, a)) / (b - a) >= WORD_COVERAGE


def _split_span(s: Span, x0: float, x1: float, mark: dict) -> list[Span]:
    """Mark the words of ``s`` that the rule covers, splitting the run if needed."""
    if s.bbox.x0 >= x0 - 0.5 and s.bbox.x1 <= x1 + 0.5:
        return [s.model_copy(update=mark)] if s.text.strip() else [s]
    words = _word_ranges(s)
    flags = [bool(t.strip()) and _covered(x0, x1, a, b) for t, a, b in words]
    # A space is underlined when the words on both sides are.
    for i, (t, _, _) in enumerate(words):
        if not t.strip() and 0 < i < len(words) - 1:
            flags[i] = flags[i - 1] and flags[i + 1]
    if not any(flags):
        return [s]
    pieces: list[Span] = []
    start = 0
    for i in range(1, len(words) + 1):
        if i == len(words) or flags[i] != flags[start]:
            a, b = words[start][1], words[i - 1][2]
            text = "".join(t for t, _, _ in words[start:i])
            update = {
                "text": text,
                "bbox": BBox(x0=a, y0=s.bbox.y0, x1=b, y1=s.bbox.y1),
                "origin": (s.origin[0] if start == 0 else a, s.origin[1]),
            }
            if flags[start]:
                update.update(mark)
            pieces.append(s.model_copy(update=update))
            start = i
    return pieces


def detect_underlines(
    lines: list[Line], drawings: list[DrawingBlock]
) -> tuple[list[Line], list[DrawingBlock], int]:
    """Return (lines, drawings_left, n_rules_turned_into_underlines)."""
    kept: list[DrawingBlock] = []
    used = 0
    for d in drawings:
        geo = _rule_geometry(d)
        if geo is None:
            kept.append(d)
            continue
        x0, x1, top, thickness, color = geo
        best, best_drop = None, float("inf")
        for i, ln in enumerate(lines):
            size = ln.size
            drop = top - ln.baseline
            if not (-0.5 <= drop <= MAX_DROP * size) or thickness > MAX_THICKNESS * size:
                continue
            slack = max(2.0, 0.5 * size)
            if x0 < ln.bbox.x0 - slack or x1 > ln.bbox.x1 + slack:
                continue
            if drop < best_drop:
                best, best_drop = i, drop
        if best is None:
            kept.append(d)
            continue
        ln = lines[best]
        mark = {"underline": True, "underline_offset": round(top - ln.baseline, 3),
                "underline_thickness": round(thickness, 3)}
        spans: list[Span] = []
        for s in ln.spans:
            overlaps = s.bbox.x1 > x0 and s.bbox.x0 < x1
            spans.extend(_split_span(s, x0, x1, mark) if overlaps and s.color.lower() == color else [s])
        if not any(s.underline for s in spans):
            kept.append(d)
            continue
        lines[best] = ln.model_copy(update={"spans": spans})
        used += 1
    return lines, kept, used
