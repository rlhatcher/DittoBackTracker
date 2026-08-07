"""Detect, mount, write to, and release the Ditto+.

Only BT.WAV is ever written or removed. LOOP.WAV is the user's own recording
and must never be touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple

from . import config, media


class PedalError(Exception):
    pass


def present() -> bool:
    """Is the pedal enumerated? by-label is created by udev automatically."""
    return config.PEDAL_DEV.exists()


def mounted() -> bool:
    return os.path.ismount(str(config.MOUNT))


def mount() -> None:
    if mounted():
        return
    if not present():
        raise PedalError("pedal not connected")
    config.MOUNT.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["mount", str(config.MOUNT)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise PedalError(f"mount failed: {r.stderr.strip()}")


def unmount() -> None:
    """Flush then unmount. The flush is what makes the last write stick."""
    if not mounted():
        return
    os.sync()
    r = subprocess.run(["umount", str(config.MOUNT)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        # Retry once after a beat; a lingering handle is the usual cause.
        import time
        time.sleep(1.0)
        os.sync()
        r = subprocess.run(["umount", str(config.MOUNT)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise PedalError(f"unmount failed: {r.stderr.strip()}")


def slot_dir(slot: int) -> Path:
    return config.MOUNT / config.SLOT_DIR.format(slot)


def track_path(slot: int) -> Path:
    return slot_dir(slot) / config.TRACK_FILENAME


def has_loop(slot: int) -> bool:
    """Did the user record a loop in this slot? Never touch it."""
    return (slot_dir(slot) / config.LOOP_FILENAME).exists()


def occupied_slots() -> Dict[int, int]:
    """slot -> BT.WAV size, for slots that actually have a backing track."""
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


def detect_format() -> Tuple[Dict, str]:
    """Probe a pedal-written file for the exact target format.

    Prefer an existing BT.WAV — a backing track the pedal has accepted is a
    better template than a recorded loop, in case the two differ.
    """
    if not mounted():
        return dict(config.DEFAULT_FORMAT), "default (pedal not mounted)"

    candidates = []
    for n in range(1, config.SLOTS + 1):
        d = slot_dir(n)
        bt, loop = d / config.TRACK_FILENAME, d / config.LOOP_FILENAME
        if bt.is_file():
            candidates.append((0, bt))
        if loop.is_file():
            candidates.append((1, loop))
    candidates.sort(key=lambda t: t[0])

    for _, path in candidates[:8]:
        info = media.probe(path)
        if info and info.codec in media.PCM_CODECS:
            return ({"sample_rate": info.sample_rate,
                     "channels": info.channels,
                     "codec": info.codec},
                    f"probed from {path.name}")
    return dict(config.DEFAULT_FORMAT), "default (nothing to probe)"


def capacity() -> Tuple[int, int]:
    """(free_bytes, total_bytes) on the pedal."""
    if not mounted():
        return (0, 0)
    st = os.statvfs(str(config.MOUNT))
    return (st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize)


def write_track(slot: int, wav: Path) -> None:
    """Copy a staged WAV into the slot as BT.WAV.

    Temp file then rename, so a truncated WAV never appears in a slot. The
    rename is exactly the operation that fails to stick without a flush, so
    the caller must unmount cleanly afterwards.
    """
    dest = track_path(slot)
    dest.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent),
                                    prefix="~bt", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with open(wav, "rb") as fsrc, os.fdopen(fd, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst, length=1 << 19)
            fdst.flush()
            os.fsync(fdst.fileno())
        os.chmod(tmp, 0o644)
        tmp.replace(dest)
        try:
            dirfd = os.open(str(dest.parent), os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        except OSError:
            pass    # not supported on all FAT drivers; os.sync() covers it
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def remove_track(slot: int) -> None:
    """Remove only BT.WAV. A recorded LOOP.WAV in the same slot survives."""
    p = track_path(slot)
    if p.is_file():
        p.unlink()


def clean_temp_files() -> int:
    """Delete leftover ~bt*.tmp files from writes interrupted by power loss.

    They are invisible to the pedal but consume its very limited capacity.
    Returns the number removed.
    """
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
