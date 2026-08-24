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

## Looked at, not planned

**Other Ditto models.** Bigger than it looks. The X2 holds *one* backing track
in a `TRACK/` folder and plays whichever file was added last; the X4 holds
*two*, in `TRACK1/` and `TRACK2/`, one per LOOP control. Neither has slots. The
99-slot map, the library-to-slot assignment, filling a folder into a run of
slots and dragging to reorder are all modelling something those pedals do not
have — so this is a second product sharing a converter, not a port.

The slot *count* is already parameterised, for what it is worth: nothing spells
out 99 and clients read `slot_count` from the snapshot. The layout is not, and
it reaches past `pedal.py` into `detect_format()`, the grid's column count and
the `slots` schema. The five places, the layouts and their sources are in
[pedal-format.md](pedal-format.md#other-models), all secondhand and unconfirmed
on hardware.

The first thing anyone trying it would hit is smaller and more annoying than
any of that: both pedals present as `DITTO`, and `config.PEDAL_LABEL` is one
label per process. So a build cannot serve both, and cannot tell which of the
two is attached without reading the volume.
