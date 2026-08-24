"""Hold docs/api.md to the routes it claims to describe.

The doc is hand-written and nothing has ever checked it. Today it happens to be
exact — every route documented, nothing documented that isn't a route — and
that is precisely the state worth pinning, because the drift is silent in both
directions. A route added without a section is undocumented and nobody notices
until someone needs it; a section left behind after a route is renamed is worse
than no doc, because it reads as true.

Same rule as test_frontend.py: objectively decidable from the files, and
already one edit from going wrong silently.
"""

import re
from pathlib import Path

import ditto

API_MD = Path(__file__).resolve().parent.parent / "docs" / "api.md"
DOC = API_MD.read_text()

# The page and its assets are not API, and are covered by their own tests in
# test_library_api.py. Named individually rather than matched by prefix: a
# pattern would quietly excuse a new route that happened to fit it.
NOT_API = {"GET /", "GET /static/<*>"}


def _normalise(rule: str) -> str:
    """Collapse every path parameter to one placeholder.

    The doc writes <n>, <hash> and <id>; Flask writes <int:slot>, <h> and
    <folder_id>. Both are naming the same thing — a parameter — and neither
    spelling is more correct, so compare the shape and let the names differ.
    """
    return re.sub(r"<[^>]+>", "<*>", rule)


def _documented() -> set:
    out = set()
    for line in DOC.splitlines():
        m = re.match(r"^## (GET|POST|PATCH|DELETE|PUT) (\S+)", line)
        if not m:
            continue                      # a prose section, like "## Errors"
        method, path = m.group(1), m.group(2)
        # Headings carry the optional query string a route accepts —
        # "[?force]", "[?start=n]" — which is documentation, not routing.
        path = path.split("[")[0]
        path = path.replace("&lt;", "<").replace("&gt;", ">")
        out.add(f"{method} {_normalise(path)}")
    return out


def _routed(app) -> set:
    out = set()
    for rule in app.url_map.iter_rules():
        for method in rule.methods:
            # Werkzeug adds these to every rule; no API documents them.
            if method in ("HEAD", "OPTIONS"):
                continue
            out.add(f"{method} {_normalise(rule.rule)}")
    return out - NOT_API


def test_every_route_is_documented(app):
    """A route with no section in api.md. The endpoint works, and the only
    record of it is the source — which is the state the doc exists to end."""
    missing = sorted(_routed(app) - _documented())
    assert not missing, f"routes with no section in docs/api.md: {missing}"


def test_every_documented_route_exists(app):
    """A section in api.md for a route that isn't there. Worse than the other
    direction: a reader follows it, gets a 404, and has no way to tell whether
    they or the doc is wrong."""
    extra = sorted(_documented() - _routed(app))
    assert not extra, f"documented in docs/api.md but not routed: {extra}"


def test_the_doc_shows_the_version_this_build_reports():
    """api.md's example /api/state response carries a literal version string.
    Bumping __version__ without editing it leaves the doc quietly a release
    behind, and the example is the first thing anyone reads."""
    shown = re.search(r'"version":\s*"([^"]+)"', DOC)
    assert shown, 'no "version" example found in docs/api.md'
    assert shown.group(1) == ditto.__version__, (
        f'docs/api.md shows version "{shown.group(1)}", '
        f'ditto.__version__ is "{ditto.__version__}"')
