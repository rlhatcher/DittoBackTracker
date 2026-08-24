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

**Other Ditto models.** The X2 and X4 use a `TRACK/` folder instead of 99
numbered directories. The target format is already detected at runtime, so most
of the work is slot-path conventions in `pedal.py`.
