"""Front-end invariants decidable from the files: that the script parses, and
that every asset the page asks for is on the allowlist. No JavaScript runner,
and deliberately not a rendering test."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ditto import web

STATIC = Path(__file__).resolve().parent.parent / "ditto" / "static"
CSS = (STATIC / "app.css").read_text()
HTML = (STATIC / "index.html").read_text()


def test_the_page_script_parses():
    """A syntax error in the script clears every gate this project has: the
    update's import check proves the Python is sound and says nothing about
    the page, which would load and never run."""
    node = shutil.which("node")
    if node is None:
        # A laptop without node skips; CI must not.
        if os.environ.get("DITTO_STRICT_FRONTEND"):
            pytest.fail("node is missing but DITTO_STRICT_FRONTEND is set")
        pytest.skip("node is not installed")
    r = subprocess.run([node, "--check", str(STATIC / "app.mjs")],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr.strip()


def test_everything_the_page_asks_for_is_on_the_asset_allowlist():
    """Assets are served from an allowlist, not a directory, so adding a file
    to static/ is two edits and forgetting the second one 404s. The reverse
    direction catches an allowlist entry for a file that was renamed."""
    refs = set(re.findall(r"/static/([\w.-]+)", HTML + CSS))
    unlisted = sorted(r for r in refs if r not in web.ASSETS)
    assert not unlisted, f"referenced by the page but not on ASSETS: {unlisted}"
    for name in web.ASSETS:
        assert (STATIC / name).is_file(), f"{name} is on ASSETS but not on disk"
