"""Configuration. Everything overridable by environment for testing."""

import os
import re
from pathlib import Path


def _abs_path(var: str, default: str) -> Path:
    """Resolved by a systemd unit with no meaningful cwd, so a relative
    override would land somewhere unexpected."""
    raw = os.environ.get(var, default).strip()
    if not raw:
        raise ValueError(f"{var} must not be empty")
    p = Path(raw)
    if not p.is_absolute():
        raise ValueError(f"{var} must be an absolute path, got {raw!r}")
    return p


DATA = _abs_path("DITTO_DATA", "/var/lib/ditto")
SOURCES = DATA / "sources"      # the uploads, by content hash
STAGED = DATA / "staged"        # converted WAVs, a cache
DB_PATH = DATA / "state.db"

# Over-the-air self-update: the git checkout the device tracks, and the deployed
# copy it runs from. Both on the data partition, provisioned once.
SRC = DATA / "src"
APP = DATA / "app"
UPDATE_BRANCH = os.environ.get("DITTO_UPDATE_BRANCH", "main")

# The label goes into a device path and a mount call, so it is held to what a
# FAT label can be.
PEDAL_LABEL = os.environ.get("DITTO_PEDAL_LABEL", "DITTOPLUS")
if not re.fullmatch(r"[A-Za-z0-9_.-]{1,11}", PEDAL_LABEL) or PEDAL_LABEL in (".", ".."):
    raise ValueError("DITTO_PEDAL_LABEL must be 1-11 chars of [A-Za-z0-9_.-]")
PEDAL_DEV = Path(f"/dev/disk/by-label/{PEDAL_LABEL}")
MOUNT = _abs_path("DITTO_MOUNT", "/media/ditto")

# Two files per slot. BT.WAV is ours to write and remove; LOOP.WAV is the
# user's recording, removed only on an explicit request.
TRACK_FILENAME = "BT.WAV"
LOOP_FILENAME = "LOOP.WAV"
SLOT_DIR = "{:02d}track"
SLOTS = 99

# The format the Ditto+ plays, measured on the pedal (docs/pedal-format.md).
SAMPLE_RATE = 44100
CHANNELS = 1
CODEC = "pcm_s24le"
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * 3

AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
                  ".aif", ".aiff", ".wma", ".opus"}

# Web
PORT = int(os.environ.get("DITTO_PORT", "80"))
POLL_SECS = 2.0              # pedal detection interval
# Whole-request cap for the upload endpoints; Flask answers 413 past it.
_max_upload_mb = int(os.environ.get("DITTO_MAX_UPLOAD_MB", "512"))
if _max_upload_mb <= 0:
    raise ValueError("DITTO_MAX_UPLOAD_MB must be a positive integer")
MAX_UPLOAD_BYTES = _max_upload_mb * 1024 * 1024


def ensure_dirs() -> None:
    for d in (SOURCES, STAGED):
        d.mkdir(parents=True, exist_ok=True)
