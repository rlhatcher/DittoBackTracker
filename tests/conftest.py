"""Point the app's data/mount paths at throwaway temp dirs before anything in
the ditto package imports config (which resolves them at import time). This
keeps a test run from touching /var/lib/ditto or /media/ditto — neither of
which exists or is writable on CI."""

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="ditto-test-")
# Unconditional, not setdefault: a dev shell that exports a real DITTO_MOUNT
# would otherwise let the filesystem tests (test_pedal calls remove_loop) act on
# a real pedal. Force throwaway temp paths for every run.
os.environ["DITTO_DATA"] = os.path.join(_tmp, "data")
os.environ["DITTO_MOUNT"] = os.path.join(_tmp, "mount")
