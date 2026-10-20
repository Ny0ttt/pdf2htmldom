"""Intermediate representation (IR) for reconstructed documents.

Design rules this module encodes:

* Text content and coordinates ALWAYS come from the PDF (or OCR). AI may decide
  *structure*; it never invents characters or positions.
* The IR is the only mutable artifact. HTML/CSS is a pure rendering of it, so the
  refinement loop and user edits can never fight each other.
* All geometry is in PDF points (1pt = 1/72in), top-left origin. CSS also
  understands ``pt``, so the mapping to CSS is 1:1 with no scaling arithmetic.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

ElementKind = Literal["heading", "para", "cell", "header", "footer", "caption", "other"]
Source = Literal["pdf", "ocr", "vision", "glyph"]


class BBox(BaseModel):
    """Axis-aligned box in PDF points, top-left origin."""

    x0: float
    y0: float
    x1: float
    y1: float

    @classmethod
    def from_seq(cls, seq) -> "BBox":
        """Build from any 4-sequence, as produced by PyMuPDF."""
        x0, y0, x1, y1 = seq
        return cls(x0=float(x0), y0=float(y0), x1=float(x1), y1=float(y1))

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    def intersects(self, other: "BBox") -> bool:
        return not (self.x1 <= other.x0 or other.x1 <= self.x0 or self.y1 <= other.y0 or other.y1 <= self.y0)

    def contains(self, other: "BBox", tol: float = 0.0) -> bool:
        return (
            other.x0 >= self.x0 - tol
            and other.y0 >= self.y0 - tol
            and other.x1 <= self.x1 + tol
            and other.y1 <= self.y1 + tol
        )

    def union(self, other: "BBox") -> "BBox":
        return BBox(
            x0=min(self.x0, other.x0),
            y0=min(self.y0, other.y0),
            x1=max(self.x1, other.x1),
            y1=max(self.y1, other.y1),
        )


class Span(BaseModel):
    """A run of characters sharing one style. Mirrors PyMuPDF's span dict."""

    text: str
    font: str
    size: float
    color: str  # "#rrggbb"
    bold: bool = False
    italic: bool = False
    serif: bool = False
    mono: bool = False
    bbox: BBox
    origin: tuple[float, float]  # baseline origin of the first character
    ascender: float  # as a fraction of size
    descender: float  # as a fraction of size (negative)


class Line(BaseModel):
    """One visual line of text. The unit of positional precision."""

    id: str
    bbox: BBox
    baseline: float
    origin_x: float
    spans: list[Span] = Field(default_factory=list)
    source: Source = "pdf"

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)

    @property
    def size(self) -> float:
        return max((s.size for s in self.spans), default=0.0)

    @property
    def ascender(self) -> float:
        return max((s.ascender for s in self.spans), default=0.8)


class TextBlock(BaseModel):
    """A group of lines that belong together, carrying semantic intent.

    Positioned as a block; individual lines land correctly via an explicit
    ``line-height`` plus forced line breaks, so the semantic tag never moves
    content away from its measured position.
    """

    type: Literal["text"] = "text"
    id: str
    kind: ElementKind = "para"
    level: int | None = None  # 1..6 for headings
    bbox: BBox
    lines: list[Line] = Field(default_factory=list)
    reading_order: int = 0
    confidence: float = 1.0
    source: Source = "pdf"
    list_style: Literal["none", "ul", "ol"] = "none"

    @property
    def text(self) -> str:
        return "\n".join(ln.text for ln in self.lines)


class TableCell(BaseModel):
    row: int
    col: int
    rowspan: int = 1
    colspan: int = 1
    bbox: BBox
    lines: list[Line] = Field(default_factory=list)
    is_header: bool = False

    @property
    def text(self) -> str:
        return " ".join(ln.text for ln in self.lines)


class TableBlock(BaseModel):
    """A real <table>. Ruled tables are detected deterministically, no AI."""

    type: Literal["table"] = "table"
    id: str
    bbox: BBox
    n_rows: int
    n_cols: int
    col_edges: list[float]  # x of each column boundary, n_cols + 1 entries
    row_edges: list[float]  # y of each row boundary, n_rows + 1 entries
    cells: list[TableCell] = Field(default_factory=list)
    reading_order: int = 0
    confidence: float = 1.0
    source: Source = "pdf"
    strategy: str = "lines"  # lines | lines_strict | text
    rule_width: float = 0.7  # measured stroke width of the original's rules, in pt


class DrawingBlock(BaseModel):
    """A vector path we did not fold into a table or a CSS border."""

    type: Literal["drawing"] = "drawing"
    id: str
    bbox: BBox
    fill: str | None = None
    stroke: str | None = None
    width: float = 0.0
    n_items: int = 1
    is_rect: bool = True
    path_d: str | None = None  # real geometry, for non-rectangular paths
    even_odd: bool = False
    reading_order: int = 0
    confidence: float = 1.0
    source: Source = "pdf"


class ImageBlock(BaseModel):
    """A raster image extracted from the PDF, referenced by asset filename."""

    type: Literal["image"] = "image"
    id: str
    bbox: BBox
    asset: str  # filename under assets/
    width_px: int
    height_px: int
    reading_order: int = 0
    confidence: float = 1.0
    source: Source = "pdf"


class BoxRowBlock(BaseModel):
    """A row of form boxes (a checkbox, or one row of a character grid),
    rebuilt from the loose rules a PDF draws them with."""

    type: Literal["boxrow"] = "boxrow"
    id: str
    bbox: BBox  # outer edge of the whole row
    count: int  # number of boxes
    box_width: float  # outer width of one box; neighbours overlap by rule_width
    box_height: float
    rule_width: float
    color: str
    reading_order: int = 0
    confidence: float = 1.0
    source: Source = "pdf"


class FrameBlock(BaseModel):
    """An outline rebuilt from loose rules: a section frame (4 sides) or a
    signature/date bracket (left, right and bottom only)."""

    type: Literal["frame"] = "frame"
    id: str
    bbox: BBox  # outer edge, borders included
    rule_width: float
    color: str
    top: bool = True  # False for a bracket open at the top
    reading_order: int = 0
    confidence: float = 1.0
    source: Source = "pdf"


Element = Annotated[
    Union[TextBlock, TableBlock, DrawingBlock, ImageBlock, BoxRowBlock, FrameBlock],
    Field(discriminator="type"),
]


class Page(BaseModel):
    number: int  # 1-based
    width: float  # points, after rotation normalisation
    height: float
    rotation: int = 0
    elements: list[Element] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Document(BaseModel):
    """The full IR. Serializable, and the only thing the pipeline mutates."""

    schema_version: int = 1
    source_file: str
    page_count: int
    pages: list[Page] = Field(default_factory=list)
    fonts_used: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def page(self, number: int) -> Page:
        return self.pages[number - 1]
