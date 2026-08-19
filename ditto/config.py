"""Configuration. Everything overridable by environment for testing."""

import os
import re
from pathlib import Path


def _abs_path(var: str, default: str) -> Path:
    """These are resolved by a systemd unit with no meaningful cwd, so a
    relative override would silently land somewhere unexpected."""
    raw = os.environ.get(var, default).strip()
    if not raw:
        raise ValueError(f"{var} must not be empty")
    p = Path(raw)
    if not p.is_absolute():
        raise ValueError(f"{var} must be an absolute path, got {raw!r}")
    return p


DATA = _abs_path("DITTO_DATA", "/var/lib/ditto")
SOURCES = DATA / "sources"
STAGED = DATA / "staged"
TRASH = DATA / "trash"
# Transient staging for loop downloads: a LOOP.WAV is copied here off the pedal,
# streamed to the browser, then purged. Nothing durable lives here.
LOOPS = DATA / "loops"
DB_PATH = DATA / "state.db"

# Over-the-air self-update. SRC is the git checkout the device tracks; APP is the
# deployed copy the service runs from (WorkingDirectory). Both live on the
# writable data partition, provisioned once — not created by ensure_dirs.
SRC = DATA / "src"
APP = DATA / "app"
# Full commit SHA of the code actually deployed to APP, written only after a
# successful swap so it never reports code that failed to land. The UI shows a
# short form and the update check compares against it.
REVISION_FILE = APP / "REVISION"
# Branch the device follows. A device is an appliance that tracks one branch.
UPDATE_BRANCH = os.environ.get("DITTO_UPDATE_BRANCH", "main")
# A oneshot unit that restarts ditto-web from outside the web process, so the
# restart isn't killing the thing that triggered it. Started via a NOPASSWD
# sudoers rule (see etc/99-ditto-restart); code changes never touch /etc.
RESTART_SERVICE = "ditto-restart.service"

# Pedal
PEDAL_LABEL = os.environ.get("DITTO_PEDAL_LABEL", "DITTOPLUS")
# The label is interpolated into a device path and later handed to privileged
# mount calls, so restrict it to what a FAT label can actually hold — a `/` or
# `..` would resolve to a different device node.
# FAT/FAT32 volume labels are at most 11 characters, so a longer one could pass
# validation yet never match the pedal.
if not re.fullmatch(r"[A-Za-z0-9_.-]{1,11}", PEDAL_LABEL) or PEDAL_LABEL in (".", ".."):
    raise ValueError("DITTO_PEDAL_LABEL must be 1-11 chars of [A-Za-z0-9_.-]")
PEDAL_DEV = Path(f"/dev/disk/by-label/{PEDAL_LABEL}")
MOUNT = _abs_path("DITTO_MOUNT", "/media/ditto")

# The pedal keeps two files per slot. We write and remove only BT.WAV; LOOP.WAV
# (the user's own recording) is read for download and removed only on an
# explicit user action, never as a side effect.
TRACK_FILENAME = "BT.WAV"
LOOP_FILENAME = "LOOP.WAV"
SLOT_DIR = "{:02d}track"
SLOTS = 99

# Measured on a Ditto+. Overridden at runtime by probing a pedal-written file.
DEFAULT_FORMAT = {"sample_rate": 44100, "channels": 1, "codec": "pcm_s24le"}

AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
                  ".aif", ".aiff", ".wma", ".opus"}

# Web
PORT = int(os.environ.get("DITTO_PORT", "80"))
POLL_SECS = 2.0              # pedal detection interval
# Upper bound the loop-download handler waits for the worker to stage a loop off
# the pedal before giving up with a 503. Must clear the worst case: a whole-pedal
# loop is ~8 min at 1 MB/s, so this leaves margin above that plus a little FIFO
# backlog. Generous because a stalled wait only ties up one waitress thread on a
# single-user box.
LOOP_STAGE_TIMEOUT = 600.0
TRASH_KEEP_DAYS = 30
# How long a stranded upload temporary in DATA must sit before the sweep takes
# it. Comfortably longer than any single upload request can run — an in-flight
# upload owns its temp file, and reaping one underneath the handler would fail
# the upload for no reason.
UPLOAD_TEMP_KEEP_SECS = 3600.0
# Grace period before the collector will touch a source or staged file. upload()
# writes the file into sources/ and only then inserts the slot row, so there is a
# window in which the file exists and nothing references it yet. A collector run
# landing inside that window would delete the upload out from under the request.
GC_GRACE_SECS = 300.0
# Whole-request cap for the two upload endpoints. A single lossless source is a
# few tens of MB; this leaves headroom for a batch while refusing a runaway
# body that would fill the small data partition. Flask answers 413 past it.
# Must be positive — a zero or negative limit would 413 every upload.
_max_upload_mb = int(os.environ.get("DITTO_MAX_UPLOAD_MB", "512"))
if _max_upload_mb <= 0:
    raise ValueError("DITTO_MAX_UPLOAD_MB must be a positive integer")
MAX_UPLOAD_BYTES = _max_upload_mb * 1024 * 1024


def ensure_dirs() -> None:
    for d in (SOURCES, STAGED, TRASH, LOOPS):
        d.mkdir(parents=True, exist_ok=True)
