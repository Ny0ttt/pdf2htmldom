"""Generate the fixture PDFs before tests are collected.

They are authored, so their geometry is ground truth. Generating them here,
rather than in a session fixture, matters because some tests decide at
collection time whether they can run (the outlined-text fixture needs Arial).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def pytest_sessionstart(session) -> None:
    spec = importlib.util.spec_from_file_location("make_fixtures", FIXTURES / "make_fixtures.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main()
