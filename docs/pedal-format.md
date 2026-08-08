# What the Ditto+ expects

Measured on a real pedal in August 2026. None of it is in TC Electronic's
documentation.

---

## USB

| | |
|---|---|
| USB ID | `1220:00bf`, "TC Electronic Ditto Plus" |
| Class | Composite: two Audio-class interfaces plus one Mass Storage interface |
| Speed | Full speed, 12 Mbps |
| Volume label | `DITTOPLUS` |
| Filesystem | FAT |
| Port | mini-USB B |
| Power | Its own 9 V supply. It does not draw meaningfully from VBUS |

It presents as a USB audio interface as well as storage. Nothing here uses
that.

Full speed caps transfers at roughly 1 MB/s, so a five-minute backing track
takes about 40 seconds to write regardless of host. The pedal is the
bottleneck.

---

## Audio format

```text
44100 Hz, 24-bit, mono   (pcm_s24le)
132,300 bytes/second  =  7.57 MiB per minute
```

Mono, not stereo: the Ditto+ has one input and one output. Published sources say
"44.1 kHz WAV" and stop there. The bit depth and channel count were measured.

### Converting

```bash
ffmpeg -i in.mp3 -vn -ar 44100 -ac 1 -c:a pcm_s24le \
       -map_metadata -1 -fflags +bitexact -f wav out.wav.part
```

Both metadata flags are needed. `-map_metadata -1` on its own still leaves
ffmpeg writing a `LIST`/`INFO` chunk containing its version string.
`-fflags +bitexact` produces a bare `RIFF/WAVE/fmt/data`.

`-f wav` is needed here because the output name ends `.part`, and ffmpeg infers
the container from the extension. Writing directly to `.wav` doesn't need it.

### RIFF header

The pedal writes a classic PCM `fmt ` chunk (tag `0x0001`) padded to 740 bytes
with a vendor block containing the string `tc electronic`:

```text
00000000: 5249 4646 f860 0b00 5741 5645 666d 7420  RIFF.`..WAVEfmt
00000010: e402 0000 0100 0100 44ac 0000 cc04 0200  ................
          ^^^^^^^^^ 740
                    ^^^^ tag 1 (PCM)
                         ^^^^ 1 channel
                              ^^^^^^^^^ 44100 Hz
00000020: 0300 1800 0000 0000 7463 2065 6c65 6374  ........tc elect
00000030: 726f 6e69 6300 0000 0000 0000 0000 0000  ronic...........
```

ffmpeg emits `WAVE_FORMAT_EXTENSIBLE` instead — tag `0xFFFE`, 40-byte `fmt ` —
for 24-bit audio.

The pedal plays ffmpeg's output anyway, so no header rewriting is needed. If
that changes, copy the pedal's own 740-byte chunk verbatim; the format is fixed,
so only the RIFF and data sizes vary.

---

## Layout

All 99 slot directories exist on a factory pedal:

```text
/01track/
/02track/
   ...
/99track/
```

Each holds up to two files, and they coexist:

| File | Written by | Contents |
|---|---|---|
| `BT.WAV` | you | Backing track |
| `LOOP.WAV` | the pedal | A loop recorded on the pedal |

Writing `LOOP.WAV` would destroy a recording. A slot with both plays the backing
track with the recorded loop over it, which is the point of the feature.

---

## Capacity

```text
~477 MiB total  ≈  63 minutes of audio  ≈  twelve five-minute tracks
```

Capacity is the real limit, not the 99 slots. Any tool for this pedal should
show remaining time rather than remaining slots.

---

## Release behaviour

The pedal does not return to looper operation while the cable is attached:

| Attempt | Result |
|---|---|
| `umount` | Filesystem unmounted, device stays enumerated |
| `eject /dev/sdX` | Refused; it doesn't advertise removable media |
| `sg_start --stop` | No effect |
| `echo 1 > /sys/bus/usb/devices/1-1/remove` | Device disappears, won't return without a replug |
| Rebinding the `dwc_otg` host controller | Segfaults the driver and takes the USB bus down until reboot |

Unplugging is the only reliable way to release it. That's why DittoBackTracker
is plugged in for a session and unplugged afterwards rather than living on the
pedalboard.

---

## Other notes

**Very short files may be ignored.** The author of
[ditto_plus_metronome](https://github.com/dummkauf/ditto_plus_metronome)
reports that a one-beat file above 140 BPM (under about 0.43 s) isn't detected,
and says they don't know whether the cause is file size or duration. Their
workaround is to generate four beats instead of one.

**macOS writes junk to the volume.** Finder adds `._` AppleDouble files,
`.Spotlight-V100` and `.fseventsd` to the FAT filesystem. `mdutil -i off
/Volumes/DITTOPLUS` stops Spotlight indexing it, and `dot_clean -m
/Volumes/DITTOPLUS` removes the AppleDouble files before ejecting.
`COPYFILE_DISABLE=1` prevents them from `cp` and `tar`, but not from Finder.

**Flush before unplugging.** On FAT, a rename in particular doesn't reliably
stick without an explicit `sync`.
