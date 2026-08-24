# Roadmap

## Not yet built

**Authentication.** The web UI is open to anyone on the network, and the only
guard in front of a state-changing request is the cross-site check in
`web.py` — which stops another origin's page acting through your browser, and
stops nothing typed at a terminal. Anyone who can route to port 80 can upload,
clear slots, download a recorded loop, force a restart onto the tracked branch,
or shut the device down.

Since 0.4.0 that reach stops at the `ditto-svc` account rather than root, so
this is now the last thing standing open rather than the second. Fine for a
home LAN, not for anything else.

The shape is not decided. The awkward part is not the login page, it is that
`GET /api/events`, the loop download and the audio preview are all reached by
the browser rather than by `fetch` — an `EventSource` cannot set a header, and
neither can an `<a download>` or an `<audio src>`. So a bearer token in a
header is out, and it comes down to a cookie or a query parameter.

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
