# CLAUDE.md

Read [CONTRIBUTING.md](CONTRIBUTING.md) first. It holds the rules, and they are
the same whoever is writing.

What matters most here, because it is the least like a default:

- **`git blame` is the documentation.** Diff size is a real cost. Prefer a small
  change with a good commit message over a tidy sweep. Don't reformat, don't
  reorder functions, don't modernise annotations for their own sake.
- **Mutation-check every test you add.** Break the thing, watch it fail, restore.
  Report that you did it, and what the failure message said.
- **Verify before you fix.** A reported defect is usually real; the stated
  consequence often isn't. Reproduce it first, and say so.
- **Don't claim what you didn't run.** Anything involving a pedal, a mount or a
  poweroff cannot be tested here. Say what needs a device instead of implying it
  was covered.

Useful commands:

```bash
.venv/bin/python -m pytest -q          # the suite, ~40s
.venv/bin/ruff check ditto/ tests/     # what CI gates on
node --check ditto/static/app.js       # also runs as a test
shellcheck install.sh
```

Running it without hardware, from the README:

```bash
DITTO_DATA=/tmp/ditto-data DITTO_MOUNT=/tmp/ditto-mount \
  .venv/bin/python -m ditto --host 127.0.0.1 --port 8080 --debug
```

Don't push, open a PR, or ask for a review without being asked to.
