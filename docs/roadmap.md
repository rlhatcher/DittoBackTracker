# Roadmap

## Not yet built

**Authentication.** The web UI is open to anyone on the network. Fine for a home
LAN, not for anything else.

**Privilege separation.** The service runs as `ditto`, the Raspberry Pi Imager
login account, which is in the `sudo` group. The scoped rules in `etc/` describe
what the app intends to do rather than bounding what it could. A dedicated
system account would fix it, at the cost of a migration touching `install.sh`,
the unit file, the fstab `uid=`/`gid=` and the data partition's ownership on
every provisioned device. Worth pairing with authentication, since anyone who
can reach `POST /api/update` can already run code from the tracked branch.

## Worth considering

**Other Ditto models.** The X2 and X4 are reported to keep tracks in a single
`TRACK/` folder rather than 99 numbered directories — reported, not measured,
unlike everything else in [pedal-format.md](pedal-format.md). The slot *count*
is already parameterised: nothing spells out 99, and clients read `slot_count`
from the snapshot. The layout is not, and it reaches further than `pedal.py` —
`detect_format()` finds a file to probe by walking the numbered directories, the
slot map's ten columns are ten because 99 divides into them, and `db.slots.slot`
is an `INTEGER PRIMARY KEY` that the move/swap parks at `-1`. The four places
are listed in [pedal-format.md](pedal-format.md#other-models). Needs one of
those pedals to develop against.
