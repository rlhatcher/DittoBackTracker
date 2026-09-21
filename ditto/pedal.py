"""Detect, mount, write to and release the Ditto+.

BT.WAV is the only file this writes, and it is written and removed freely
because it is re-derivable from sources/. LOOP.WAV is the user's own recording:
read for a download, removed only on an explicit request.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, Tuple

from . import config

log = logging.getLogger(__name__)

# A wedged USB device makes mount(8) block indefinitely, and the monitor thread
# would block with it.
_MOUNT_TIMEOUT = 30.0


class PedalError(Exception):
    pass


def _run(cmd, what: str) -> subprocess.CompletedProcess:
    """Run a mount helper, turning a timeout or a missing binary into a
    PedalError the monitor can show rather than a traceback every poll."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=_MOUNT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise PedalError(f"{what} timed out after {int(_MOUNT_TIMEOUT)}s") from None
    except OSError as e:
        raise PedalError(f"{what} could not run: {e}") from None


def present() -> bool:
    """Is the pedal enumerated? udev creates the by-label link."""
    return config.PEDAL_DEV.exists()


def mounted() -> bool:
    return os.path.ismount(str(config.MOUNT))


def mount() -> None:
    if mounted():
        return
    if not present():
        raise PedalError("pedal not connected")
    config.MOUNT.mkdir(parents=True, exist_ok=True)
    r = _run(["mount", str(config.MOUNT)], "mount")
    if r.returncode != 0:
        raise PedalError(f"mount failed: {r.stderr.strip()}")


def unmount() -> None:
    """Flush then unmount. The flush is what makes the last write stick."""
    if not mounted():
        return
    os.sync()
    r = _run(["umount", str(config.MOUNT)], "unmount")
    if r.returncode != 0:
        # Retry once after a beat; a lingering handle is the usual cause.
        time.sleep(1.0)
        os.sync()
        r = _run(["umount", str(config.MOUNT)], "unmount")
        if r.returncode != 0:
            raise PedalError(f"unmount failed: {r.stderr.strip()}")


def slot_dir(slot: int) -> Path:
    return config.MOUNT / config.SLOT_DIR.format(slot)


def track_path(slot: int) -> Path:
    return slot_dir(slot) / config.TRACK_FILENAME


def loop_path(slot: int) -> Path:
    return slot_dir(slot) / config.LOOP_FILENAME


def has_loop(slot: int) -> bool:
    return loop_path(slot).is_file()


def occupied_slots() -> Dict[int, int]:
    """slot -> BT.WAV size, for slots that have a backing track."""
    out = {}
    if not mounted():
        return out
    for n in range(1, config.SLOTS + 1):
        p = track_path(n)
        try:
            if p.is_file():
                out[n] = p.stat().st_size
        except OSError:
            pass
    return out


def capacity() -> Tuple[int, int]:
    """(free_bytes, total_bytes) on the pedal."""
    if not mounted():
        return (0, 0)
    st = os.statvfs(str(config.MOUNT))
    return (st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize)


def write_track(slot: int, wav: Path) -> None:
    """Copy a staged WAV into the slot as BT.WAV.

    Temp file, fsync, rename: the pedal never sees a truncated file, and a
    power cut leaves either the old file or the new one. The caller runs
    os.sync() afterwards, which is what makes the rename stick on FAT.
    """
    dest = track_path(slot)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), prefix="~bt",
                                    suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fdst, open(wav, "rb") as fsrc:
            shutil.copyfileobj(fsrc, fdst, length=1 << 19)
            os.fchmod(fdst.fileno(), 0o644)   # before the sync, so it lands too
            fdst.flush()
            os.fsync(fdst.fileno())
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def remove_track(slot: int) -> None:
    """Remove only BT.WAV. A recorded LOOP.WAV in the same slot survives."""
    p = track_path(slot)
    if p.is_file():
        p.unlink()


def remove_loop(slot: int) -> None:
    """Remove a slot's LOOP.WAV by exact name. Never touches BT.WAV."""
    p = loop_path(slot)
    if p.is_file():
        p.unlink()


def clean_temp_files() -> int:
    """Delete ~bt*.tmp files left by interrupted writes. Invisible to the
    pedal, but they consume its capacity. Returns the number removed."""
    if not mounted():
        return 0
    removed = 0
    for n in range(1, config.SLOTS + 1):
        d = slot_dir(n)
        if not d.is_dir():
            continue
        try:
            for p in d.glob("~bt*.tmp"):
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed
