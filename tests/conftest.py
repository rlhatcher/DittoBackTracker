"""Point the app's data/mount paths at throwaway temp dirs before anything in
the ditto package imports config (which resolves them at import time). This
keeps a test run from touching /var/lib/ditto or /media/ditto — neither of
which exists or is writable on CI."""

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="ditto-test-")
os.environ.setdefault("DITTO_DATA", os.path.join(_tmp, "data"))
os.environ.setdefault("DITTO_MOUNT", os.path.join(_tmp, "mount"))
