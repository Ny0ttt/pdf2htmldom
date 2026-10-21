# pdf2htmldom

**Rebuild PDF pages as real, editable HTML elements, placed where the PDF puts them, and checked in a real browser.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

 pdf2htmldom builds each page from native elements: headings, paragraphs, lists and real `<table>`s, positioned on a page-sized canvas. It opens the result in Chromium, **measures** where every line actually landed, corrects it, and reports how close it got.

---

## Table of contents

- [Overview](#overview)
- [Features](#features)
- [Demo](#demo)
- [Technology stack](#technology-stack)
- [Prerequisites](#prerequisites)
- [Quick start](#quick-start)
- [Usage](#usage)
- [Testing](#testing)
- [Known limitations](#known-limitations)
- [Contributing](#contributing)
- [License](#license)
- [Acknowledgments](#acknowledgments)

---

## Overview

pdf2htmldom turns a PDF into an HTML package you can edit, search and select, while keeping the page looking like the original.

**How it works:**

```
PDF ─► extract ─► tables, form boxes ─► layout ─► document.json ─► HTML/CSS ─► measure in Chromium ─► correct ─► validate
```

**Who it is for:** developers who need PDFs, especially forms and documents with ruled tables, as maintainable HTML rather than images or loose text.

**What sets it apart:**

- **Geometry is measured, not assumed.** A zero-size probe reports each line's real baseline in the browser, so corrections are exact translations, not pixel guesses.
- **Every run is scored.** Each conversion reports text preservation, line position error, SSIM and ink coverage against acceptance targets, marked `PASS` or `MISS`. Misses are reported, never hidden.
- **Outlined text is read back exactly.** When a PDF has turned its text into vector shapes (common with Microsoft Publisher exports), the letters are matched against the fonts installed on your machine rather than guessed by OCR.

## Features

- **Semantic HTML:** `<h1>`–`<h6>`, `<p>`, `<ol>`/`<ul>`/`<li>`, `<table>` with real `rowspan`/`colspan`, and `<img>`. No page-sized raster, no canvas, no JavaScript in the output.
- **Baseline-accurate text placement.** CSS `pt` equals the PDF point, so coordinates are copied over with no scaling, then refined in Chromium for a set number of passes (`--max-iters`).
- **Ruled table detection** with merged cells, deterministic (no AI). A structural validator rejects prose that only looks like a table.
- **Text converted to outlines** is recovered by matching each letter's shape against installed font files. One affine fit per letter recovers size, horizontal squeeze and synthetic italic slant, so text, weight, italic, colour and baseline come out exact. Letters that match nothing fall back to OCR and are reported.
- **OCR** with Tesseract for scanned pages, and for single outlined regions that could not be matched.
- **Form reconstruction:** checkboxes and character grids drawn as loose hairlines are rebuilt as `.box-row` / `.form-box` elements. Section outlines and signature lines become `.frame` / `.bracket`.
- **Clean output:** one element per text line in page coordinates, a handful of utility classes (`.b`, `.i`, `.n`, `.w`, `.r`, …), no `data-*` attributes, and `white-space: nowrap`, so re-formatting the file cannot break the layout.
- **Editable table borders:** hide or thicken individual cell lines with classes (`no-r`, `no-lr`, `no-x`, …) or per-side variables (`--bt`, `--br`, `--bb`, `--bl`). See [Customising table borders](#customising-table-borders).
- **Validation report** per page: SSIM, ink recall and precision, baseline error, a text-preservation check, plus original, generated, diff and side-by-side PNGs.
- **Single-file export** (`--single-file`) with CSS and images inlined.
- **Print styles** that match the original page size.
- **Local web UI:** upload a PDF, convert, preview the scores and side-by-side images, then open the output folder.

## Demo

Example CLI run on the bundled `table_basic` fixture (a ruled table with merged cells):

```text
  fixtures\table_basic.pdf -> out\table_basic   (1.5s total)
    [PASS] text_preserved_pct       value=100.0  target=100.0
    [PASS] line_error_mean_pt       value=0.01  target=1.5
    [PASS] line_error_max_pt        value=0.014  target=4.0
    [PASS] ssim_min                 value=0.9436  target=0.92
    [PASS] ink_recall_min           value=0.9999  target=0.98
    [PASS] width_issues             value=0  target=0
```

> **Screenshot placeholder:** add a side-by-side image (for example `docs/side-by-side.png`, from `page-1-side-by-side.png` of a fixture run) and a web UI screenshot here.

## Technology stack

| Area | Used for | Required? |
|---|---|---|
| Python ≥ 3.10 | Pipeline and orchestration | Yes |
| [PyMuPDF](https://pymupdf.readthedocs.io/) | Text, geometry, image and vector extraction; page rendering | Yes (installed by pip) |
| [Playwright](https://playwright.dev/python/) + Chromium | Rendering, in-browser geometry measurement, screenshots | Yes. Without Chromium, conversions can't run |
| [OpenCV](https://opencv.org/), NumPy, Pillow | Visual comparison (SSIM, ink masks), pixel analysis | Yes (installed by pip) |
| [Pydantic](https://docs.pydantic.dev/) v2 | The intermediate representation (`document.json`) | Yes (installed by pip) |
| [fontTools](https://github.com/fonttools/fonttools) | Reading glyph outlines from installed fonts | Yes (installed by pip) |
| [Tesseract](https://github.com/tesseract-ocr/tesseract) via `pytesseract` | OCR | Optional, recommended. Without it, scanned pages and unmatched outlined text aren't OCR'd. The test suite still passes. |
| [Flask](https://flask.palletsprojects.com/) | The local web UI | Optional (`ui` extra) |
| pytest | Test suite | Optional (`dev` extra) |
| The fonts your PDFs use | Matching outlined text; rendering the HTML | Optional, matters a lot (see below) |

**Why fonts matter.** Some PDFs, notably Microsoft Publisher exports, store text as vector outlines instead of characters. pdf2htmldom reads them back by matching each letter against the font files installed on **your** machine: Arial, Arial Narrow, Calibri and Times New Roman by default, plus any installed family the PDF names. With the font installed, text, style and position come out exact. Without it, that text falls back to OCR (and its usual misreads) and is flagged in the report. Fonts also decide how the HTML renders.

- **Windows** ships Arial, Calibri and Times New Roman. Arial Narrow is usually installed with Microsoft Office.
- **Ubuntu** doesn't. `ttf-mscorefonts-installer` (requires accepting Microsoft's EULA) adds Arial and Times New Roman, but not Arial Narrow or Calibri.

`pdf2htmldom doctor` tells you which of these your machine has.

## Prerequisites

### Supported platforms

| Platform | Status |
|---|---|
| Windows 11, x64 | **Verified:** developed and tested here, including a clean install from the instructions below. |
| Linux (Ubuntu) | **Untested.** The Ubuntu instructions below follow each tool's documented setup, but haven't been run yet. Outlined-text quality depends on fonts, see below. Reports welcome. |
| macOS | **Untested.** The code has macOS paths, but nobody has run them yet. Reports welcome. |

Python **3.10 or newer** is required. Only **3.12** has been verified; `constraints.txt` pins the versions tested with it. For what each dependency does and which are optional, see [Technology stack](#technology-stack).

## Quick start

### Windows

**1. Set up** (once). In PowerShell:

```powershell
# Download the source code and enter the project folder
git clone https://github.com/Ny0ttt/pdf2htmldom.git
cd pdf2htmldom

# Create an isolated Python environment in .venv
python -m venv .venv

# Install pdf2htmldom and its dependencies
.venv\Scripts\python.exe -m pip install -e ".[ui,dev]" -c constraints.txt

# Download the Chromium browser used for rendering and measuring
.venv\Scripts\python.exe -m playwright install chromium

# Optional: install Tesseract for OCR of scanned pages
winget install UB-Mannheim.TesseractOCR
```

**2. Check the setup:**

```powershell
.venv\Scripts\pdf2htmldom.exe doctor
```

It should end with `Ready.` (see [Checking your setup](#checking-your-setup)).

**3. Run it.** From the project folder, either start the web UI:

```powershell
.venv\Scripts\pdf2htmldom-ui.exe
```

and open <http://127.0.0.1:5000>, or convert from the command line:

```powershell
.venv\Scripts\pdf2htmldom.exe build path\to\document.pdf -o out\my-doc
```

### Linux (Ubuntu)

**1. Set up** (once):

```bash
# Install Python's venv module, plus Tesseract for OCR (tesseract-ocr is optional)
sudo apt-get install -y python3-venv tesseract-ocr

# Optional: install Arial and Times New Roman
sudo apt-get install -y ttf-mscorefonts-installer

# Download the source code and enter the project folder
git clone https://github.com/Ny0ttt/pdf2htmldom.git
cd pdf2htmldom

# Create an isolated Python environment in .venv
python3 -m venv .venv

# Install pdf2htmldom and its dependencies
.venv/bin/python -m pip install -e ".[ui,dev]" -c constraints.txt

# Download Chromium, plus the system libraries it needs to launch
.venv/bin/python -m playwright install --with-deps chromium
```

`--with-deps` matters on Linux: without those system libraries, Chromium often fails to launch.

**2. Check the setup:**

```bash
.venv/bin/pdf2htmldom doctor
```

It should end with `Ready.` (see [Checking your setup](#checking-your-setup)).

**3. Run it.** From the project folder, either start the web UI:

```bash
.venv/bin/pdf2htmldom-ui
```

and open <http://127.0.0.1:5000>, or convert from the command line:

```bash
.venv/bin/pdf2htmldom build path/to/document.pdf -o out/my-doc
```

### Checking your setup

`doctor` prints one line per requirement:

```text
[  OK ] python                  Python 3.12.10 (Windows AMD64)
[  OK ] pymupdf                 1.28.2
…
[  OK ] chromium                launches (version 156.0.8078.4)
[  OK ] tesseract               5.4.0.20240606; languages: eng, osd
[  OK ] font: Arial             regular, bold, italic, bold italic
…
Ready.
```

**FAIL** lines must be fixed: each one prints the fix. **WARN** lines mean conversions run with reduced quality. The command exits with code 1 only if something failed.

### Setup notes

- **`-c constraints.txt`** pins the exact versions the test suite is verified with. Leave it out and pip installs the newest versions the ranges in `pyproject.toml` allow; those usually work, but haven't been checked.
- Extras: `ui` adds Flask for the web UI, and `dev` adds pytest. For the CLI alone, `pip install -e . -c constraints.txt` is enough.
- Tesseract on other systems: see the [Tesseract installation guide](https://tesseract-ocr.github.io/tessdoc/Installation.html). If it isn't on `PATH`, pdf2htmldom checks the usual Windows install locations, or you can set `TESSERACT_CMD` (see [Configuration](#configuration)).

## Usage

The examples below use the short command names `pdf2htmldom` and `pdf2htmldom-ui`. They work as-is once the venv is activated (`.venv\Scripts\Activate.ps1` on Windows, `source .venv/bin/activate` on Linux). Otherwise, use the full `.venv` paths from [Quick start](#quick-start).

### Command line

```bash
# Convert a PDF into an HTML package in out/my-doc
pdf2htmldom build path/to/document.pdf -o out/my-doc

# Also write a single self-contained HTML file (CSS and images inlined)
pdf2htmldom build path/to/document.pdf -o out/my-doc --single-file

# Only some pages, and skip OCR (faster; text layer only)
pdf2htmldom build path/to/document.pdf -o out/my-doc --pages 1,3-5 --no-ocr

# Print the full report as JSON instead of the summary
pdf2htmldom build path/to/document.pdf -o out/my-doc --json
```

`python -m pdf2htmldom.cli build …` works the same.

All `build` options:

| Option | Default | What it does |
|---|---|---|
| `-o`, `--out` | *(required)* | Output folder for the HTML package |
| `--pages` | all | Pages to convert, e.g. `1,3-5` |
| `--single-file` | off | Also write `standalone.html` with CSS and images inlined |
| `--no-ocr` | off | Skip OCR (faster; text layer only) |
| `--ocr-dpi` | `300` | Render resolution for OCR |
| `--max-iters` | `3` | Maximum measure-and-correct passes in Chromium |
| `--scale` | `1.5` | Device scale factor for the validation screenshots |
| `--fit-ocr-size` | off | Experimental: also fit OCR'd line width by scaling the type. Doesn't converge reliably yet |
| `--json` | off | Print the full report as JSON instead of the summary |

The summary shows per-page scores, the text-preservation result, each acceptance target as `PASS`/`MISS`, and the first warnings (rotated text skipped, tables continuing across pages, regions left to OCR, …).

### Web UI

```bash
pdf2htmldom-ui
```

Then open <http://127.0.0.1:5000>.

1. Drop a PDF onto the page, or click to choose one.
2. Optionally set a page range or turn OCR off.
3. Click **Convert**. The checks, font substitutions, warnings and side-by-side previews appear underneath.
4. **Open folder** opens the output folder in your file manager. **Open HTML** opens the result in a new tab.

Each conversion gets its own folder, `out/<pdf-name>-<timestamp>/`, holding `original.pdf` and the full output package. The server only listens on `127.0.0.1` and runs one conversion at a time.

### Configuration

Two optional environment variables:

| Variable | Default | What it does |
|---|---|---|
| `TESSERACT_CMD` | *(auto)* | Full path to the `tesseract` executable, if it isn't on `PATH` or in the usual Windows install locations |
| `PDF2HTMLDOM_OUT` | `out` | Folder where the web UI saves conversions |

```powershell
# Windows (PowerShell)
$env:TESSERACT_CMD = "D:\Tools\Tesseract-OCR\tesseract.exe"
```

```bash
# Linux
export PDF2HTMLDOM_OUT=/srv/pdf2htmldom-out
```

### Customising table borders

Generated tables are ready to edit by hand. Every cell border defaults to the table's `--rule` value, but that default has zero specificity (`:where(.tbl) :where(td,th)`), so **any class or inline style you put on a cell overrides it**. The output's CSS (`styles.css`, or the `<style>` block in `standalone.html`) also includes the `no-*` classes below.

**Borders belong to the cell.** Put classes and styles on the `<td>`/`<th>`, not on the `<div>` or `<span>` inside it.

**Hide a line**: add a class. Classes combine:

| Class | Hides |
|---|---|
| `no-l` / `no-r` / `no-lr` | left / right / both |
| `no-t` / `no-b` / `no-tb` | top / bottom / both |
| `no-x` | all four |

```html
<td class="no-lr">…</td>
<td class="no-r no-b">…</td>
```

**Change a line's thickness**: set a variable on the cell:

| Variable | Side |
|---|---|
| `--bt` / `--br` / `--bb` / `--bl` | top / right / bottom / left |

```html
<td style="--bb:3pt">…</td>               <!-- thick bottom -->
<td class="no-t" style="--bb:3pt">…</td>  <!-- mix both -->
```

**Rows and the whole table**: add rules to the CSS:

```css
.tbl tr:nth-child(4) > td { border-bottom: hidden; }   /* no line under row 4 */
.tbl tr:nth-child(2) > td { --bt: 3pt; }               /* thick line above row 2 */
.tbl { border: 3pt solid #000; }                       /* thick outer frame */
```

`style="--rule:1pt"` on the `<table>` changes the default thickness of every line.

**How collapsed borders behave.** Neighbouring cells share each line, and the thicker border wins:
- **Thicker** works from one cell.
- **Thinner** only works when set on *both* cells that share the line.
- To **remove** a line, use a `no-*` class. Setting a side to `0` doesn't work, because the neighbour still draws it.

**Avoid** column selectors like `td:nth-child(3)`. Tables with `colspan` put a different column at that position on each row, so put classes on the actual cells.




## Testing

```bash
python -m pytest -rs
```

- Fixture PDFs are generated automatically before the tests run (`tests/conftest.py`). Their geometry is authored, so every assertion is a measured property with exact ground truth.
- The outlined-text tests need **Arial** installed; they are skipped when it isn't. `-rs` shows each skip and its reason. Lines in fonts you don't have (often Arial Narrow and Calibri on Linux) are left out of that fixture.
- Tesseract is **not** needed for the suite to pass.
- One test is an expected failure (`xfail`): SSIM on pages dense with hairline rules lands just under the 0.92 target. That's a documented, measured miss (see [Known limitations](#known-limitations)).
- No linter or formatter is configured yet.

## Known limitations

- **Fonts are substituted, not embedded.** The closest installed face is used and every substitution is listed in `report.json`.
- **Text inside table cells** is placed from the first line onwards in normal flow, and row heights are not yet fixed. Cells with blank lines between paragraphs can drift vertically against neighbouring cells.
- **SSIM on rule-dense pages** sits around 0.90–0.94 against a 0.92 target. Chromium rounds collapsed hairline borders differently from the PDF renderer.
- **Rotated text** is detected and skipped, with a warning.
- **Multi-column reading order** is detected and flagged, and is provisional.
- **Tables that continue across pages** are flagged, not joined.
- **Outlined text:**
  - Only fonts installed on the machine can be matched. Symbol fonts shipped as `.ttc` collections are not read yet, so those letters go to OCR.
  - Justified lines with wide word gaps are kept as separately positioned pieces.
  - Recovery takes about 2 seconds on a dense one-page form.
- **PyMuPDF 2.x** changes extraction behaviour and has not been validated, so the dependency is pinned to `<2`.
- **macOS** is untested.


## Contributing

Contributions are welcome: bug reports, fixes, and fixtures that reproduce real-world problems. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for the development setup, the test workflow, and the project's few rules. The main one: test fixtures are generated, never real documents.

For a bug report, include the output of `pdf2htmldom doctor`, plus the `report.json` and `page-N-side-by-side.png` from the conversion.

> **Maintainer placeholder:** no code of conduct exists yet.


## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 Rasyid Hamidun.

## Acknowledgments

Built on [PyMuPDF](https://github.com/pymupdf/PyMuPDF), [Playwright](https://github.com/microsoft/playwright-python), [OpenCV](https://github.com/opencv/opencv), [fontTools](https://github.com/fonttools/fonttools), [Tesseract](https://github.com/tesseract-ocr/tesseract), [Pydantic](https://github.com/pydantic/pydantic) and [Flask](https://github.com/pallets/flask).
