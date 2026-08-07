# Roadmap

## Not yet built

**Enclosure.** The stack is about 20 mm tall over a 65 × 30 mm footprint, and
the charge port, slide switch and USB port all need to stay reachable.

**Reading loops off the pedal.** Mounting and format detection already exist, so
this is mostly a UI question. It would make the device a backup tool as well as
a loader.

**Authentication.** The web UI is open to anyone on the network. Fine for a home
LAN, not for anything else.

## Worth considering

**Other Ditto models.** The X2 and X4 use a `TRACK/` folder instead of 99
numbered directories. The target format is already detected at runtime, so most
of the work is slot-path conventions in `pedal.py`.

**A minimum-length warning.** There is a report that the pedal ignores very
short files, though neither the threshold nor the cause is established — see
[pedal-format.md](pedal-format.md#other-notes). A warning would help, because
the failure looks the same as a format problem.

**Crossfade on upload.** The pedal loops whatever it is given, so a track that
doesn't loop cleanly clicks at the seam.

**Setlist import.** Fill slots in order from a folder or playlist, stopping at
the 63-minute limit.

## Untested

The button, LED and display are implemented and detected at startup, but have
not been run against real hardware. The SSD1306 driver was written for this
project.
