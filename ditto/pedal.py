"""Detect, mount, write to, and release the Ditto+.

BT.WAV is the only file this tool writes, and it is written and removed freely
because it is re-derivable from sources/. LOOP.WAV is the user's own recording,
with no source and no way to reconstruct it: it is only ever read (to stage a
download) or removed on an explicit, deliberate user action — never as a side
effect of anything else.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, Tuple

from . import config, media

log = logging.getLogger(__name__)

# A wedged or half-unplugged USB device makes mount(8) block indefinitely.
# The monitor thread calls mount() and the shutdown path calls umount(), so an
# untimed call there would freeze pedal detection or stop the device powering
# off — better to surface it as an error the panel can show.
_MOUNT_TIMEOUT = 30.0


class PedalError(Exception):
    pass


def _run(cmd, what: str) -> subprocess.CompletedProcess:
    """Run a mount helper, turning every failure into a PedalError.

    OSError matters as much as the timeout: a missing or non-executable
    mount(8) raises FileNotFoundError or PermissionError, which is not a
    PedalError and so escapes the caller's handler entirely. In the monitor
    that lands in the catch-all and logs a full traceback every POLL_SECS
    forever, on a device with a read-only root and a small journal.
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=_MOUNT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise PedalError(f"{what} timed out after {int(_MOUNT_TIMEOUT)}s") from None
    except OSError as e:
        raise PedalError(f"{what} could not run: {e}") from None


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
    """Did the user record a loop in this slot? Only ever read or, on an
    explicit user action, removed — never overwritten."""
    return loop_path(slot).is_file()


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


# errnos that mean "this filesystem has no directory fsync", as opposed to
# "this write is in trouble". EINVAL is what vfat returns; ENOTSUP/EOPNOTSUPP
# cover drivers that refuse the operation outright.
#
# Kept deliberately narrow. The same handler covers the open, the fsync and the
# close, so anything wider starts absorbing failures that have nothing to do
# with an unsupported operation — EACCES and EPERM are access or policy
# problems, EBADF is a descriptor bug — and silence there would recreate exactly
# the ambiguity this set exists to remove.
_DIR_FSYNC_UNSUPPORTED = frozenset({
    errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP,
})


def _atomic_copy(src: Path, dest: Path, prefix: str) -> None:
    """Copy `src` over `dest` so `dest` is never seen half-written.

    Temp file in the destination directory, fsync, chmod, rename, then fsync
    the directory. Both directions of the pedal transfer use this — writing a
    staged WAV into a slot, and copying a recorded loop back off — because both
    have a reader that must never see a truncated file: the pedal's firmware in
    one direction, the browser in the other.

    Kept in one place deliberately. This is the durability-critical path on a
    device whose whole threat model is a pulled plug, and it was previously two
    byte-identical copies, so a correction had to land twice or land wrong.

    `prefix` names the temp file. Interrupted writes are collected by name:
    `~bt` on the pedal by clean_temp_files, `~loop` locally when the staging
    directory is purged at startup.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent),
                                    prefix=prefix, suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        # os.fdopen first so it owns and closes fd even if opening the source
        # raises — a staged file, or a loop, can vanish between check and copy.
        with os.fdopen(fd, "wb") as fdst, open(src, "rb") as fsrc:
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
        except OSError as e:
            # Plenty of FAT drivers simply do not implement fsync on a
            # directory. That is the expected case here, it is harmless, and
            # the os.sync() the caller does covers it — so it stays silent.
            #
            # A failing card is not that, and the two must not look alike. Say
            # so, but do not raise: the rename has already happened, the file
            # is in place and readable, and only its directory entry is
            # unconfirmed. Failing the write here would report a loss that did
            # not occur and would leave the caller retrying a copy that landed.
            if e.errno not in _DIR_FSYNC_UNSUPPORTED:
                log.warning("could not flush the directory entry for %s: %s. "
                            "The file is written, but may not survive a power "
                            "cut before the next sync.", dest, e)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def write_track(slot: int, wav: Path) -> None:
    """Copy a staged WAV into the slot as BT.WAV.

    Temp file then rename, so a truncated WAV never appears in a slot. The
    rename is exactly the operation that fails to stick without a flush, so
    the caller must unmount cleanly afterwards.
    """
    _atomic_copy(wav, track_path(slot), "~bt")


def copy_loop(slot: int, dest: Path) -> None:
    """Copy a slot's LOOP.WAV off the pedal into a local `dest`.

    The reverse direction of write_track: the pedal is the source, `dest` is a
    local staging file. Raises FileNotFoundError if the loop is gone (e.g.
    deleted on the pedal between has_loop() and here).
    """
    _atomic_copy(loop_path(slot), dest, "~loop")


def remove_track(slot: int) -> None:
    """Remove only BT.WAV. A recorded LOOP.WAV in the same slot survives."""
    p = track_path(slot)
    if p.is_file():
        p.unlink()


def remove_loop(slot: int) -> None:
    """Remove a slot's LOOP.WAV by exact name, on explicit user action only.

    Never touches BT.WAV, never removes the slot directory, never recurses.
    """
    p = loop_path(slot)
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
