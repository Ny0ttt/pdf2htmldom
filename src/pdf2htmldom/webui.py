"""Local web UI: upload a PDF, convert it, open the result.

    python -m pdf2htmldom.webui        # then open http://127.0.0.1:5000

Binds to 127.0.0.1 only. One route opens File Explorer on this machine, so the
server must never be reachable from the network.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

from .cli import _pages
from .pipeline import build

# Conversions land in ./out, or wherever PDF2HTMLDOM_OUT points.
OUT = Path(os.environ.get("PDF2HTMLDOM_OUT", "out")).resolve()
JOB_RE = re.compile(r"^[A-Za-z0-9_-]+$")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

# The pipeline drives a single Chromium per build; two at once would just compete
# for CPU and make both slow, so conversions are serialised.
_build_lock = threading.Lock()


def _job_dir(job: str) -> Path:
    """Resolve a job id to its folder, refusing anything that is not one."""
    if not JOB_RE.match(job):
        abort(404)
    d = OUT / job
    if not d.is_dir():
        abort(404)
    return d


def _new_job_id(filename: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(filename).stem).strip("_")[:60] or "document"
    return f"{stem}-{datetime.now():%Y%m%d-%H%M%S}"


@app.get("/")
def index():
    return send_file(Path(__file__).with_name("webui.html"))


@app.post("/convert")
def convert():
    upload = request.files.get("pdf")
    if upload is None or not upload.filename:
        return jsonify(error="No file uploaded."), 400
    if not upload.filename.lower().endswith(".pdf"):
        return jsonify(error="That isn't a PDF."), 400

    try:
        pages = _pages(request.form.get("pages", "").strip() or None)
    except ValueError:
        return jsonify(error="Page range should look like 1,3-5"), 400
    use_ocr = request.form.get("ocr") == "on"

    job = _new_job_id(upload.filename)
    out = OUT / job
    out.mkdir(parents=True)
    pdf_path = out / "original.pdf"
    upload.save(pdf_path)

    with _build_lock:
        try:
            build(
                str(pdf_path),
                out,
                page_numbers=pages,
                single_file=True,
                skip_ocr=not use_ocr,
            )
        except Exception as exc:  # surface the real error in the UI
            return jsonify(error=f"{type(exc).__name__}: {exc}", job=job), 500

    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    previews = sorted(
        (p.name for p in out.glob("page-*-side-by-side.png")),
        key=lambda n: int(n.split("-")[1]),
    )
    return jsonify(
        job=job,
        folder=str(out),
        acceptance=report.get("acceptance", {}),
        warnings=report.get("warnings", []),
        fonts=report.get("font_substitutions", {}),
        previews=previews,
    )


@app.get("/out/<job>/<path:filename>")
def output_file(job: str, filename: str):
    return send_from_directory(_job_dir(job), filename)


@app.post("/open/<job>")
def open_folder(job: str):
    d = _job_dir(job)
    target = d / "index.html"
    if sys.platform == "win32":
        # explorer.exe always exits 1, so don't check the return code.
        if target.exists():
            subprocess.Popen(f'explorer /select,"{target}"')
        else:
            os.startfile(d)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(target if target.exists() else d)])
    else:
        subprocess.Popen(["xdg-open", str(d)])
    return jsonify(ok=True)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    print("pdf2htmldom UI -> http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, threaded=True)


if __name__ == "__main__":
    main()
