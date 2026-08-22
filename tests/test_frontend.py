"""Front-end invariants that nothing else can catch.

There is no JavaScript test runner here and this does not add one. What it does
is check the handful of front-end facts that are objectively decidable from the
files themselves, chosen because each has already gone wrong or is one edit away
from going wrong silently:

  * Colour contrast. A palette that fails WCAG looks completely normal. This is
    not a hypothetical — the accent label pairs shipped in review at 3.76:1
    because the author measured them correctly and then compared against the
    wrong threshold. WCAG's large-text allowance is 18pt, or 14pt *bold*, and
    14pt bold is 18.66px, not 14px bold. The rule is encoded once, here, so the
    next person does not get to re-derive it from memory.

  * Layer discipline. An unlayered rule outranks every layered one, so a single
    stray rule at the end of app.css silently beats the whole sheet.

  * The grid column count, which app.js has to repeat for arrow-key movement.

None of this needs a browser, so it costs the suite a few milliseconds and CI
nothing. It is deliberately not a rendering test: layout is verified by eye
against the design, and pretending otherwise would be the worse lie.
"""

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "ditto" / "static"
CSS = (STATIC / "app.css").read_text()
JS = (STATIC / "app.js").read_text()


def _strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


# --------------------------------------------------------------- colour

def _hex(value: str):
    v = value.lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16), 1.0)


def _tokens(scope: str) -> dict:
    """The custom properties in effect for 'light' or 'dark'.

    The dark block only re-points some of them, so it is layered over the light
    set rather than replacing it — which is the property the stylesheet relies
    on and therefore worth reproducing rather than assuming.
    """
    blocks = re.findall(r":root\s*\{(.*?)\}", _strip_comments(CSS), flags=re.S)
    assert len(blocks) == 2, f"expected a light and a dark :root, found {len(blocks)}"
    text = blocks[0] if scope == "light" else blocks[0] + blocks[1]
    out = {}
    for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", text):
        out[name] = value.strip()
    return out


def _resolve(value: str, tokens: dict, depth: int = 0):
    """A colour as (r, g, b, alpha). Handles the three forms this sheet uses."""
    assert depth < 10, f"var() cycle resolving {value!r}"
    value = value.strip()
    if value.startswith("#"):
        return _hex(value)
    m = re.fullmatch(r"var\((--[\w-]+)\)", value)
    if m:
        return _resolve(tokens[m.group(1)], tokens, depth + 1)
    m = re.fullmatch(r"color-mix\(in srgb,\s*(.+?)\s+([\d.]+)%,\s*transparent\)", value)
    if m:
        r, g, b, _ = _resolve(m.group(1), tokens, depth + 1)
        return (r, g, b, float(m.group(2)) / 100)
    raise AssertionError(f"cannot resolve colour {value!r}")


def _over(fg, bg):
    """Composite a translucent colour onto an opaque one."""
    a = fg[3]
    return tuple(a * fg[i] + (1 - a) * bg[i] for i in range(3))


def _luminance(rgb):
    def chan(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(c) for c in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg, bg):
    lo, hi = sorted((_luminance(_over(fg, bg)), _luminance(bg)))
    return (hi + 0.05) / (lo + 0.05)


def required(px: float, weight: int) -> float:
    """WCAG 1.4.3. Large text is 18pt, or 14pt bold — 24px, or 18.66px bold.

    Points, not pixels. Reading "14 point bold" as "14px bold" is exactly the
    mistake that put a 3.76:1 label into review, so it is spelled out.
    """
    large = px >= 24 or (weight >= 700 and px >= 18.66)
    return 3.0 if large else 4.5


# Every place the page draws text, as (foreground, background, px, weight).
# Sizes are the ones in app.css; if a rule's font-size changes, change it here.
TEXT = [
    ("--color-text",     "--color-bg",          15, 400, "body copy"),
    ("--color-text",     "--color-bg",          18, 800, "the brand"),
    ("--text-muted",     "--color-bg",          13, 400, "section meta"),
    ("--text-muted",     "--color-surface",     13, 400, "the status message"),
    ("--color-text",     "--color-surface",     15, 800, "the drop zone title"),
    ("--text-muted",     "--color-surface",     12, 400, "the drop zone note"),
    ("--text-faint",     "--color-bg",          11, 400, "the version string"),
    ("--text-faint",     "--color-bg",          12, 400, "durations"),
    ("--text-danger",    "--color-bg",          13, 800, "the mid-write warning"),
    ("--text-danger",    "--color-surface",     13, 800, "the same, in the bar"),
    ("--accent-legible", "--color-bg",          12, 400, "a ghost button"),
    ("--color-bg",       "--accent-legible",    14, 800, "the primary button"),
    # the slot numbers printed in the map, one per cell state
    ("--text-faint",     "--color-bg",          10, 400, "an empty slot number"),
    ("--on-state",       "--state-synced",      10, 400, "an on-pedal slot number"),
    ("--on-state",       "--state-converting",  10, 400, "a converting slot number"),
    ("--on-queued",      "--state-queued",      10, 400, "a queued slot number"),
    ("--on-state",       "--state-error",       10, 400, "an errored slot number"),
]

# Boundaries of things you can operate, which WCAG 1.4.11 puts at 3:1.
NON_TEXT = [
    ("--cell-border", "--color-bg", "the border of an empty slot"),
]

SCHEMES = ("light", "dark")


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("fg,bg,px,weight,what", TEXT,
                         ids=[t[4].replace(" ", "-") for t in TEXT])
def test_every_piece_of_text_meets_wcag_aa(scheme, fg, bg, px, weight, what):
    """Text the eye can read is not the same as text that passes, and neither
    the reviewer nor the author reliably tells them apart by looking."""
    tokens = _tokens(scheme)
    ratio = contrast(_resolve(f"var({fg})", tokens), _resolve(f"var({bg})", tokens))
    need = required(px, weight)
    assert ratio >= need, (
        f"{what} in {scheme}: {fg} on {bg} is {ratio:.2f}:1 at {px}px/{weight}, "
        f"needs {need}:1")


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("fg,bg,what", NON_TEXT, ids=[t[2].replace(" ", "-") for t in NON_TEXT])
def test_control_boundaries_meet_wcag_non_text_contrast(scheme, fg, bg, what):
    """The design's own divider token measures 2.41:1 here, which is why the
    cell border is a separate token — this is what stops it drifting back."""
    tokens = _tokens(scheme)
    ratio = contrast(_resolve(f"var({fg})", tokens), _resolve(f"var({bg})", tokens))
    assert ratio >= 3.0, f"{what} in {scheme}: {ratio:.2f}:1, needs 3:1"


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
    assert not leftover.strip(), f"these rules sit outside every layer:\n{leftover.strip()}"


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


def test_the_arrow_keys_step_the_same_grid_the_css_draws():
    """app.js repeats the column count so Up and Down can move a whole row. The
    two have to agree or Down lands on the wrong slot, and nothing errors."""
    css = int(re.search(r"#grid\s*\{[^}]*repeat\((\d+),", _strip_comments(CSS)).group(1))
    js = int(re.search(r"const GRID_COLS\s*=\s*(\d+)", _strip_comments(JS)).group(1))
    assert js == css, f"GRID_COLS is {js} but the CSS draws {css} columns"
