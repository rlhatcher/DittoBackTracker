# Contributing

These are the rules this codebase already follows. They were only ever recorded
in commit messages and file docstrings, which meant they survived exactly as
long as anyone kept reading those.

## Layering

```
config → {media, db, update} → pedal → core → web
```

| Module | Owns |
|---|---|
| `config.py` | Paths and constants |
| `db.py` | SQLite storage |
| `media.py` | ffprobe and ffmpeg |
| `pedal.py` | Detect, mount, write `BT.WAV`, unmount |
| `update.py` | Over-the-air self-update: git, systemd |
| `core.py` | Session lifecycle and work queue |
| `web.py` | Flask routes and server-sent events |

Acyclic, and it stays that way. New code goes in the layer that owns the
resource: SQL in `db.py` and nowhere else, subprocess calls to ffmpeg in
`media.py`, mount and unmount in `pedal.py`.

`web.py` reads from `db` directly for a handful of routes. That is allowed and
the reason is written at the call site. The test for which side a route belongs
on: **if losing power mid-request could leave the device inconsistent, it goes
through the service**, because the admission lock is the only thing that orders
work against a shutdown. Everything else can read straight through.

Don't reach past a layer. `web.py` used to call `pedal.mounted()`; it asks
`service.mounted` now. Don't call another module's `_private` either. If you
need it, it wasn't private.

## Tests

Two kinds, and they answer different questions.

**Behaviour**, mostly against a real `Service` on a throwaway data tree. The
interesting behaviour lives in the service rather than the route, so drive it
through HTTP and let the worker threads run. `tests/test_library_api.py` is the
model.

**Pins**, for facts that are objectively decidable from the files and already
one edit from going wrong silently. `tests/test_frontend.py` and
`tests/test_docs.py` are entirely this. A pin earns its place when the failure
it prevents is invisible: a stale API doc, a contrast ratio, a regex the client
and server both keep a copy of, an asset that 404s into a fallback font. If
breaking the thing would produce a loud error anyway, it doesn't need a pin.

**Mutation-check every new test.** Break the thing it pins, watch it fail, read
the message, restore. Commit first so the restore is `git stash` or an editor
undo rather than `git checkout` over uncommitted work. A test that has never
failed has not been tested.

Some things are stubbed on purpose and enforced rather than trusted:

- ffmpeg and ffprobe never run in the suite. `conftest._block_ffmpeg` raises if
  one is reached.
- `sudo` never runs. `conftest._block_sudo` records the argv instead, so a test
  cannot power off the machine it is running on.

## Comments

Explain the decision and what it rejected, not the mechanism. The code says
what it does; the comment says why it isn't the other way.

A comment that would be true of any project's code isn't worth the line. A
comment naming the failure that produced the code is worth several.

## Commits

The message is the design record. `git blame` and `git log` are the only
documentation of why most of this looks the way it does, which is also why there
is no formatter here (see below).

- Subject in the imperative, naming the behaviour change rather than the
  mechanism. "Send the slot number that was typed, not a number", not "Fix
  fillFolder".
- Body: what was wrong, how it was reproduced, what got pinned so it stays
  fixed, and any alternative considered and rejected.
- End with `Suite: N tests.`

## Style

`ruff check ditto/ tests/` gates CI. The rules are chosen in `pyproject.toml`
and the reasoning is there too. A deliberate exception gets a per-line
`# noqa: RULE` with its reason, never a global ignore. An ignore silences the
rule in new code as well, and the point is to record *this* decision.

**`ruff format` is not used, deliberately.** There are no style arguments to
settle here, and reformatting 21 files would rewrite the hand-wrapped comments
that carry the design record. `.git-blame-ignore-revs` only helps GitHub blame;
`git log -L`, `git log -S` and most editors ignore it.

`node --check` parses `app.js` in the suite. There is no JS linter and no
`package.json`: the front end's real invariants are contrast, layer discipline
and client/server constant parity, and those are tests, not lint rules.

## Docs

`docs/api.md` is part of the API. `tests/test_docs.py` fails if a route has no
section or a section has no route, so a route change is not done until the doc
changes.

`ditto/__init__.py.__version__` is the single source of truth for the version.
It is not in `pyproject.toml`, and it is not read from `importlib.metadata`.
The device is deployed by copying `ditto/` and never pip-installed, so there is
no `.dist-info` to read: that would raise on every Pi while passing CI.

A release is: bump `__version__`, update the example in `docs/api.md` (a test
fails if you forget), commit, and `git tag -a vX.Y.Z`. The device keeps
comparing commit SHAs against the branch it tracks and does not look at tags.

## Front end

One file, on purpose. `ditto/static/app.js` stays a single classic script with
no build step, no bundler and no ES modules: each module would cost a round trip
on a page served `no-cache` from a Pi Zero, serialise on an import graph the
preload scanner can't see, and need its own `ASSETS` allowlist entry where
forgetting one is a silent 404. The map at the top of the file lists the
sections. Revisit if it passes ~2500 lines.

Adding a file to `static/` is two edits: the file, and `ASSETS` in `web.py`. A
test catches the second if you forget.

Colours come from the token layer in `app.css`. A component naming a raw ramp
step fails the suite, and so does any text pair below its WCAG threshold. The
table in `test_frontend.py` is hand-maintained, so add a row when you add a new
foreground/background pairing.

## Hardware

Anything touching `install.sh`, the systemd units or the read-only overlay can't
be tested here and shouldn't pretend otherwise. Say in the commit what you ran
on a device: pedal mounts, pedal unmounts cleanly on stop, a track writes, Done
powers off, Update restarts.
