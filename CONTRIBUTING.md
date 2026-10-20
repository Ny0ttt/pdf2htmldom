# Contributing to pdf2htmldom

Thanks for helping. This page covers the development setup and the few rules the project relies on.

## Development setup

Follow [Installation](README.md#installation) in the README, then check your machine:

```bash
pdf2htmldom doctor
```

`doctor` reports Python, packages, Chromium, Tesseract and fonts. **FAIL** means conversions won't run. **WARN** means they run with reduced quality: for example, no Tesseract means no OCR, and a missing font means outlined text in that font falls back to OCR.

Always install with the constraints file:

```bash
python -m pip install -e ".[ui,dev]" -c constraints.txt
```

It pins the versions the test suite is verified with, so your results match everyone else's.

## Running the tests

```bash
python -m pytest -rs
```

`-rs` lists skipped tests and the reason. Skips you might see:

| Skip | Why | To run it |
|---|---|---|
| Outlined-text tests | Arial isn't installed (common on Linux) | Install Microsoft's core fonts (Ubuntu: `ttf-mscorefonts-installer`) |

One `xfail` is expected: SSIM on rule-dense pages is a documented, measured miss (see [Known limitations](README.md#known-limitations)).

There's no CI, so run the full suite locally before opening a pull request, and paste the summary line into the pull request.

## Rules

**1. Test fixtures are generated, never real documents.**
Every fixture is drawn by `fixtures/make_fixtures.py`, so its geometry is known exactly and nothing private ever enters the repository. If a real PDF shows a bug, reduce it to an authored fixture that reproduces the same structure (a table shape, a font trick, a drawing pattern). Don't commit the PDF. `.gitignore` excludes `*.pdf` and `out/` to help with this.

**2. Measure, don't assert.**
Tests check measured properties (positions in points, recovered text, scores against the acceptance targets in `pipeline.py`), not "looks right". A change that affects output should say what it did to `report.json` metrics on the fixtures.

**3. Report misses honestly.**
If a change makes a metric worse somewhere, say so in the pull request. A known miss is marked `xfail` with the measured reason, not loosened until it passes.

**4. Keep `constraints.txt` in sync.**
When you change a dependency, or a newer version needs to be pinned, install without `-c` in a fresh virtual environment, run the full suite, and copy the passing versions into `constraints.txt`. Doing this occasionally is also how you find out an upstream release has broken something (PyMuPDF 2.0 did).

## Pull requests

1. Fork, then create a branch: `git checkout -b fix/short-description`.
2. Make the change with a test.
3. Run `python -m pytest -rs`.
4. Open the pull request. Describe the problem, the change, and how you verified it (before/after metrics if output changed).

## Reporting bugs

Open an issue with:
- the output of `pdf2htmldom doctor`;
- the `report.json` and `page-N-side-by-side.png` from the conversion;
- the PDF, **only if** you're allowed to share it and it contains nothing personal. Otherwise, describe what kind of document it is (exported from which tool, scanned or not, tables, forms).
