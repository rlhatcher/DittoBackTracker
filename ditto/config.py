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
DB_PATH = DATA / "state.db"

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

# The pedal keeps two files per slot. We only ever touch BT.WAV.
TRACK_FILENAME = "BT.WAV"
LOOP_FILENAME = "LOOP.WAV"
SLOT_DIR = "{:02d}track"
SLOTS = 99

# Measured on a Ditto+. Overridden at runtime by probing a pedal-written file.
DEFAULT_FORMAT = {"sample_rate": 44100, "channels": 1, "codec": "pcm_s24le"}

AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
                  ".aif", ".aiff", ".wma", ".opus"}

# Power
MIN_CHARGE_PCT = 30          # refuse writes below this
CRITICAL_CHARGE_PCT = 15     # graceful halt
INA219_ADDR = 0x43
I2C_BUS = 1

# Panel
BUTTON_GPIO = int(os.environ.get("DITTO_BUTTON_GPIO", "17"))
LED_GPIO = int(os.environ.get("DITTO_LED_GPIO", "27"))
OLED_ADDR = 0x3C
OLED_HEIGHT = int(os.environ.get("DITTO_OLED_HEIGHT", "32"))
if OLED_HEIGHT not in (32, 64):
    # Anything else gives a page count the SSD1306 addressing doesn't match,
    # so the display renders garbage rather than failing.
    raise ValueError("DITTO_OLED_HEIGHT must be 32 or 64")
LONG_PRESS_SECS = 3.0

# Web
PORT = int(os.environ.get("DITTO_PORT", "80"))
POLL_SECS = 2.0              # pedal detection interval
TRASH_KEEP_DAYS = 30
# Whole-request cap for the two upload endpoints. A single lossless source is a
# few tens of MB; this leaves headroom for a batch while refusing a runaway
# body that would fill the small data partition. Flask answers 413 past it.
# Must be positive — a zero or negative limit would 413 every upload.
_max_upload_mb = int(os.environ.get("DITTO_MAX_UPLOAD_MB", "512"))
if _max_upload_mb <= 0:
    raise ValueError("DITTO_MAX_UPLOAD_MB must be a positive integer")
MAX_UPLOAD_BYTES = _max_upload_mb * 1024 * 1024


def ensure_dirs() -> None:
    for d in (SOURCES, STAGED, TRASH):
        d.mkdir(parents=True, exist_ok=True)
