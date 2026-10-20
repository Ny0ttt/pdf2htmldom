"""Form-box reconstruction.

Forms draw checkboxes and character grids ("comb" fields) as loose hairlines:
a checkbox is two horizontal and two vertical rules, a 2x31 name grid is three
long horizontals and 32 verticals. Rendered one-to-one that is correct but
uneditable -- moving a checkbox means moving four elements in lockstep.

This pass rebuilds them as rows of boxes, one ``BoxRowBlock`` per grid row.

It is deliberately conservative, and the guarantee is visual: a line is only
removed when the boxes redraw all of it, at the same position, to within
``TOL``. A missed box costs nothing (the rules stay as they were); a wrongly
recognised box draws the same edges, so it is a naming error, never a drawing
error.
"""

from __future__ import annotations

from .models import BBox, BoxRowBlock, DrawingBlock, FrameBlock

TOL = 0.5  # pt; PDF producers are sloppy about where rule ends meet
MAX_BOX = 30.0  # pt; anything larger is a frame, not a form box
MIN_BOX = 3.0
ASPECT = (0.5, 2.0)  # height / width
COL_TOL = 0.25  # pt; max deviation of any column from the row's uniform pitch
MIN_RULE = 0.5  # pt; the renderer draws hairlines at least this thick


def _is_h(d: DrawingBlock) -> bool:
    return not d.path_d and d.bbox.height <= 1.6 and d.bbox.width > 8


def _is_v(d: DrawingBlock) -> bool:
    return not d.path_d and d.bbox.width <= 1.6 and d.bbox.height > 8


def _color(d: DrawingBlock) -> str:
    return (d.fill or d.stroke or "#000").lower()


def _covers(intervals: list[tuple[float, float]], lo: float, hi: float) -> bool:
    """Does the union of ``intervals`` cover [lo, hi], allowing TOL gaps?"""
    pos = lo
    for a, b in sorted(intervals):
        if a > pos + TOL:
            return False
        pos = max(pos, b)
        if pos >= hi - TOL:
            return True
    return pos >= hi - TOL


def _runs(cols: list[DrawingBlock]) -> list[list[DrawingBlock]]:
    """Split sorted columns into runs of uniform pitch.

    Every column in a run must sit within COL_TOL of where a uniform pitch puts
    it. Checking each column against the run's start, not just its neighbour,
    stops small per-step errors from accumulating across a 30-box row.
    """
    runs: list[list[DrawingBlock]] = []
    i = 0
    while i < len(cols) - 1:
        run = [cols[i], cols[i + 1]]
        x0 = cols[i].bbox.x0
        pitch = cols[i + 1].bbox.x0 - x0
        k = i + 2
        while k < len(cols):
            n = len(run)
            if abs(cols[k].bbox.x0 - (x0 + n * pitch)) > COL_TOL:
                break
            run.append(cols[k])
            pitch = (cols[k].bbox.x0 - x0) / n
            k += 1
        runs.append(run)
        i = k - 1
    return runs


def _merge_rows(rows: list[BoxRowBlock]) -> list[BoxRowBlock]:
    """Join rows that continue each other.

    Producers often draw one long grid rule as several segments (this form uses
    5-box pieces), so detection finds the grid in chunks. Two chunks are one row
    when they share an edge column and have the same height, rule and pitch --
    and the merged row still puts every original column within COL_TOL.
    """
    rows = sorted(rows, key=lambda r: (round(r.bbox.y0, 1), r.bbox.x0))
    out: list[BoxRowBlock] = []
    for r in rows:
        prev = out[-1] if out else None
        if prev is not None:
            p_pitch = prev.box_width - prev.rule_width
            r_pitch = r.box_width - r.rule_width
            joins = (
                abs(r.bbox.y0 - prev.bbox.y0) <= TOL
                and abs(r.box_height - prev.box_height) <= TOL
                and abs(r.rule_width - prev.rule_width) <= 0.05
                and r.color == prev.color
                and abs(r.bbox.x0 - (prev.bbox.x1 - prev.rule_width)) <= TOL  # shared column
            )
            if joins:
                n = prev.count + r.count
                pitch = (r.bbox.x1 - r.rule_width - prev.bbox.x0) / n
                # every column of both pieces must still land within COL_TOL
                ok = all(
                    abs(prev.bbox.x0 + k * p_pitch - (prev.bbox.x0 + k * pitch)) <= COL_TOL
                    for k in range(prev.count + 1)
                ) and all(
                    abs(r.bbox.x0 + k * r_pitch - (prev.bbox.x0 + (prev.count + k) * pitch)) <= COL_TOL
                    for k in range(r.count + 1)
                )
                if ok:
                    out[-1] = prev.model_copy(
                        update={
                            "bbox": BBox(x0=prev.bbox.x0, y0=prev.bbox.y0, x1=r.bbox.x1, y1=prev.bbox.y1),
                            "count": n,
                            "box_width": pitch + prev.rule_width,
                        }
                    )
                    continue
        out.append(r)
    return out


def detect_box_rows(
    drawings: list, page_no: int
) -> tuple[list[BoxRowBlock], list, int]:
    """Return (box rows, drawings still to render, number of rules absorbed)."""
    hs = [d for d in drawings if isinstance(d, DrawingBlock) and _is_h(d)]
    vs = [d for d in drawings if isinstance(d, DrawingBlock) and _is_v(d)]
    rows: list[BoxRowBlock] = []
    # Edges each row draws, for the coverage check: y -> [(x0, x1)], x -> [(y0, y1)].
    h_edges: list[tuple[float, float, float]] = []
    v_edges: list[tuple[float, float, float]] = []
    seen: set[tuple[int, int, int]] = set()

    for top in hs:
        for bot in hs:
            dy = bot.bbox.y0 - top.bbox.y0
            if not (MIN_BOX < dy <= MAX_BOX) or _color(bot) != _color(top):
                continue
            x0 = max(top.bbox.x0, bot.bbox.x0)
            x1 = min(top.bbox.x1, bot.bbox.x1)
            if x1 - x0 < MIN_BOX:
                continue

            cols = sorted(
                (
                    v
                    for v in vs
                    if _color(v) == _color(top)
                    and x0 - TOL <= v.bbox.x0 <= x1 + TOL
                    and v.bbox.y0 <= top.bbox.y0 + TOL
                    and v.bbox.y1 >= bot.bbox.y1 - TOL
                ),
                key=lambda v: v.bbox.x0,
            )
            # Several overlapping verticals at one x are one column.
            uniq: list[DrawingBlock] = []
            for v in cols:
                if not uniq or v.bbox.x0 - uniq[-1].bbox.x0 > TOL:
                    uniq.append(v)

            for run in _runs(uniq):
                pitch = (run[-1].bbox.x0 - run[0].bbox.x0) / (len(run) - 1)
                if not (MIN_BOX <= pitch <= MAX_BOX and ASPECT[0] <= dy / pitch <= ASPECT[1]):
                    continue
                # A horizontal between top and bot means these are two stacked
                # rows, not one tall box; the adjacent pairs will pick them up.
                left, right = run[0].bbox.x0, run[-1].bbox.x0
                if any(
                    top.bbox.y0 + TOL < h.bbox.y0 < bot.bbox.y0 - TOL
                    and h.bbox.x0 < right - TOL
                    and h.bbox.x1 > left + TOL
                    for h in hs
                ):
                    continue
                key = (round(left * 4), round(top.bbox.y0 * 4), len(run))
                if key in seen:
                    continue
                seen.add(key)

                widths = sorted([top.bbox.height, bot.bbox.height] + [v.bbox.width for v in run])
                rule = max(MIN_RULE, widths[len(widths) // 2])
                rows.append(
                    BoxRowBlock(
                        id="",
                        bbox=BBox(x0=left, y0=top.bbox.y0, x1=right + rule, y1=bot.bbox.y0 + rule),
                        count=len(run) - 1,
                        box_width=pitch + rule,
                        box_height=dy + rule,
                        rule_width=rule,
                        color=_color(top),
                    )
                )

    # Single-path boxes: some producers draw a checkbox as one stroked rectangle.
    single_rects: set[str] = set()
    for d in drawings:
        if not isinstance(d, DrawingBlock) or d.path_d or not d.is_rect or d.fill or not d.stroke:
            continue
        w, h = d.bbox.width, d.bbox.height
        if not (MIN_BOX <= w <= MAX_BOX and MIN_BOX <= h <= MAX_BOX and ASPECT[0] <= h / w <= ASPECT[1]):
            continue
        rule = max(MIN_RULE, d.width or 0.0)
        half = rule / 2
        rows.append(
            BoxRowBlock(
                id="",
                bbox=BBox(x0=d.bbox.x0 - half, y0=d.bbox.y0 - half, x1=d.bbox.x1 + half, y1=d.bbox.y1 + half),
                count=1,
                box_width=w + rule,
                box_height=h + rule,
                rule_width=rule,
                color=_color(d),
            )
        )
        single_rects.add(d.id)

    rows = _merge_rows(rows)
    rows.sort(key=lambda r: (r.bbox.y0, r.bbox.x0))
    for i, r in enumerate(rows):
        r.id = f"p{page_no}-box{i}"
        pitch = r.box_width - r.rule_width
        h_edges.append((r.bbox.y0, r.bbox.x0, r.bbox.x1))
        h_edges.append((r.bbox.y1 - r.rule_width, r.bbox.x0, r.bbox.x1))
        for k in range(r.count + 1):
            v_edges.append((r.bbox.x0 + k * pitch, r.bbox.y0, r.bbox.y1))

    # Remove only rules the boxes fully redraw. Anything partially used (a long
    # rule that also extends past the grid) stays as it is.
    kept = []
    absorbed = 0
    for d in drawings:
        if isinstance(d, DrawingBlock) and d.id in single_rects:
            absorbed += 1
            continue
        if isinstance(d, DrawingBlock) and _is_h(d):
            spans = [(a, b) for y, a, b in h_edges if abs(y - d.bbox.y0) <= TOL]
            if spans and _covers(spans, d.bbox.x0, d.bbox.x1):
                absorbed += 1
                continue
        if isinstance(d, DrawingBlock) and _is_v(d):
            spans = [(a, b) for x, a, b in v_edges if abs(x - d.bbox.x0) <= TOL]
            if spans and _covers(spans, d.bbox.y0, d.bbox.y1):
                absorbed += 1
                continue
        kept.append(d)
    return rows, kept, absorbed


# --- frames and brackets ------------------------------------------------------

FRAME_TOL = 1.0  # pt; where a frame's sides meet
FRAME_END_TOL = 1.5  # pt; how far a bottom rule may sit below the sides' ends


def _rule(d) -> str | None:
    """'h' or 'v' for a thin filled rectangle (any length >= 4pt), else None."""
    if not isinstance(d, DrawingBlock) or d.path_d or not d.is_rect:
        return None
    w, h = d.bbox.width, d.bbox.height
    if min(w, h) > 1.6 or max(w, h) < 4:
        return None
    return "h" if w > h else "v"


def _thick(d: DrawingBlock) -> float:
    return min(d.bbox.width, d.bbox.height)


def detect_frames(drawings: list, page_no: int) -> tuple[list[FrameBlock], list, int]:
    """Rebuild outlines drawn as loose rules: two sides plus a bottom, and an
    optional top. Four sides is a frame; no top is a signature/date bracket.

    Run after ``detect_box_rows`` so small boxes are already taken. Every side
    must line up with the others to within FRAME_TOL, so the frame redraws each
    rule it absorbs; anything that does not line up is left as it is.
    """
    hs = [d for d in drawings if _rule(d) == "h"]
    vs = sorted((d for d in drawings if _rule(d) == "v"), key=lambda d: d.bbox.x0)
    used: set[str] = set()
    frames: list[FrameBlock] = []

    def matching_h(left: DrawingBlock, right: DrawingBlock, y: float, at_bottom: bool):
        for h in hs:
            if h.id in used or _color(h) != _color(left) or abs(_thick(h) - _thick(left)) > 0.3:
                continue
            if abs(h.bbox.x0 - left.bbox.x0) > FRAME_TOL or abs(h.bbox.x1 - right.bbox.x1) > FRAME_TOL:
                continue
            edge = h.bbox.y1 if at_bottom else h.bbox.y0
            if abs(edge - y) <= (FRAME_END_TOL if at_bottom else FRAME_TOL):
                return h
        return None

    for i, left in enumerate(vs):
        if left.id in used:
            continue
        for right in vs[i + 1:]:
            if right.id in used or _color(right) != _color(left):
                continue
            if right.bbox.x0 - left.bbox.x0 <= MAX_BOX or abs(_thick(right) - _thick(left)) > 0.3:
                continue
            if abs(right.bbox.y0 - left.bbox.y0) > FRAME_TOL or abs(right.bbox.y1 - left.bbox.y1) > FRAME_TOL:
                continue
            bottom = matching_h(left, right, max(left.bbox.y1, right.bbox.y1), at_bottom=True)
            if bottom is None:
                continue
            top = matching_h(left, right, min(left.bbox.y0, right.bbox.y0), at_bottom=False)
            sides = [left, right, bottom] + ([top] if top else [])
            widths = sorted(_thick(s) for s in sides)
            rule = max(MIN_RULE, widths[len(widths) // 2])
            y0 = top.bbox.y0 if top else min(left.bbox.y0, right.bbox.y0)
            frames.append(
                FrameBlock(
                    id=f"p{page_no}-frame{len(frames)}",
                    bbox=BBox(x0=left.bbox.x0, y0=y0, x1=right.bbox.x0 + rule, y1=bottom.bbox.y0 + rule),
                    rule_width=rule,
                    color=_color(left),
                    top=top is not None,
                )
            )
            used.update(s.id for s in sides)
            break

    kept = [d for d in drawings if not (isinstance(d, DrawingBlock) and d.id in used)]
    return frames, kept, len(used)
