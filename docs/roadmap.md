# Roadmap

## Not yet built

**Enclosure.** None yet; the mechanical constraints are in
[hardware.md](hardware.md#enclosure).

**Authentication.** The web UI is open to anyone on the network. Fine for a home
LAN, not for anything else.

## Worth considering

**Other Ditto models.** The X2 and X4 use a `TRACK/` folder instead of 99
numbered directories. The target format is already detected at runtime, so most
of the work is slot-path conventions in `pedal.py`.

## Untested

The button, LED and display are implemented and detected at startup, but have
not been run against real hardware. The SSD1306 driver was written for this
project.
