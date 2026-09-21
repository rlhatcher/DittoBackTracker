"""Front-end invariants decidable from the files: layer discipline, the token
rule, that the script parses, that it reaches for elements that exist, and
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
JS = (STATIC / "app.js").read_text()
HTML = (STATIC / "index.html").read_text()


def _strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def test_the_stylesheet_puts_every_rule_in_a_layer():
    """An unlayered rule beats every layered one, so one stray rule at the end
    of the file would quietly outrank the whole sheet."""
    body = _strip_comments(CSS)
    # Remove each balanced top-level @layer block, then see what is left.
    out, i = [], 0
    while i < len(body):
        m = re.compile(r"@layer\s+[\w-]+\s*\{").search(body, i)
        if not m:
            out.append(body[i:])
            break
        out.append(body[i:m.start()])
        depth, j = 1, m.end()
        while depth:
            if body[j] == "{":
                depth += 1
            elif body[j] == "}":
                depth -= 1
            j += 1
        i = j
    leftover = "".join(out)
    leftover = re.sub(r"@layer[^;]*;", "", leftover)          # the order statement
    leftover = re.sub(r"@font-face\s*\{[^}]*\}", "", leftover)  # declares a resource
    assert not leftover.strip(), \
        f"these rules sit outside every layer:\n{leftover.strip()}"


def test_only_the_token_layer_names_a_raw_ramp_step():
    """Components name what a colour is for, not which step it happens to be.
    A component reaching past the semantic block into the ramp is how the token
    layer rots back into a colour dictionary."""
    body = _strip_comments(CSS)
    start = body.index("@layer tokens {")
    depth, end = 1, start + len("@layer tokens {")
    while depth:
        if body[end] == "{":
            depth += 1
        elif body[end] == "}":
            depth -= 1
        end += 1
    strays = [body[:m.start()].count("\n") + 1
              for m in re.finditer(r"color-(?:accent|neutral)-[0-9]00", body)
              if not start <= m.start() < end]
    assert not strays, f"raw ramp steps used outside @layer tokens, near lines {strays}"


def test_the_page_script_parses():
    """A syntax error in app.js clears every gate this project has.

    The over-the-air update smoke-checks a deployment by importing ditto.web
    (update.py), which proves the Python is sound and says nothing about the
    2000 lines of JavaScript beside it. The service restarts clean, /api/state
    answers, the SSE stream runs — and the browser gets a page whose script
    never ran. The 180-second "update didn't confirm" timeout cannot help,
    because the code that sets that timeout is the code that did not parse.

    Nothing in this repo has ever parsed this file except by hand.

    Syntax only: node --check will not catch a renamed identifier that one call
    site missed. That needs a linter with no-undef, which needs package.json and
    node_modules in a repo whose entire build config is five lines of TOML.
    """
    node = shutil.which("node")
    if node is None:
        # A laptop without node skips; CI must not. Without this the gate
        # becomes a permanent green skip the first time the image loses node,
        # and nobody finds out until a broken page ships.
        if os.environ.get("DITTO_STRICT_FRONTEND"):
            pytest.fail("node is missing but DITTO_STRICT_FRONTEND is set")
        pytest.skip("node is not installed")
    r = subprocess.run([node, "--check", str(STATIC / "app.js")],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr.strip()


def test_every_element_the_script_reaches_for_is_on_the_page():
    """$("#x") returns null and the next line throws — but only when that path
    first runs, which for a button in the library header is not page load. One
    id renamed in index.html is a runtime failure with no compile step to catch
    it and no test here that touches a DOM.

    One direction only. index.html carries ids that only the stylesheet uses,
    and pinning those would fail on every CSS refactor.
    """
    wanted = set(re.findall(r'\$\("#([\w-]+)"\)', _strip_comments(JS)))
    present = set(re.findall(r'id="([\w-]+)"', HTML))
    missing = sorted(wanted - present)
    assert not missing, f"app.js reaches for ids that are not in index.html: {missing}"


def test_everything_the_page_asks_for_is_on_the_asset_allowlist():
    """Assets are served from an allowlist, not a directory, so adding a file to
    static/ is two edits and forgetting the second one 404s.

    For a script that is loud. For the @font-face src it is silent: the page
    renders in a fallback face, and every contrast pair in the table above is
    then measuring a font nobody is looking at.

    The reverse direction catches the other half — an allowlist entry for a file
    that was renamed or deleted. archivo-OFL.txt is on the list and referenced
    by nothing, which is correct: the licence has to ship beside the font.
    """
    refs = set(re.findall(r"/static/([\w.-]+)", HTML + _strip_comments(CSS)))
    unlisted = sorted(r for r in refs if r not in web.ASSETS)
    assert not unlisted, f"referenced by the page but not on ASSETS: {unlisted}"
    for name in web.ASSETS:
        assert (STATIC / name).is_file(), f"{name} is on ASSETS but not on disk"
