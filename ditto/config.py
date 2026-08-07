"""Configuration. Everything overridable by environment for testing."""

import os
from pathlib import Path

DATA = Path(os.environ.get("DITTO_DATA", "/var/lib/ditto"))
SOURCES = DATA / "sources"
STAGED = DATA / "staged"
TRASH = DATA / "trash"
DB_PATH = DATA / "state.db"

# Pedal
PEDAL_LABEL = os.environ.get("DITTO_PEDAL_LABEL", "DITTOPLUS")
PEDAL_DEV = Path(f"/dev/disk/by-label/{PEDAL_LABEL}")
MOUNT = Path(os.environ.get("DITTO_MOUNT", "/media/ditto"))

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
OLED_HEIGHT = int(os.environ.get("DITTO_OLED_HEIGHT", "32"))   # 32 or 64
LONG_PRESS_SECS = 3.0

# Web
PORT = int(os.environ.get("DITTO_PORT", "80"))
POLL_SECS = 2.0              # pedal detection interval
TRASH_KEEP_DAYS = 30


def ensure_dirs() -> None:
    for d in (SOURCES, STAGED, TRASH):
        d.mkdir(parents=True, exist_ok=True)
