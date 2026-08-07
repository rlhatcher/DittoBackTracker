# Hardware

## Parts

| Part | Notes | ~Cost |
|---|---|---|
| Raspberry Pi Zero 2 W | 65×30 mm, WiFi, USB OTG host. A soldered header is only needed for the optional display | £18 |
| Waveshare UPS HAT (C) | Same footprint, pogo-pin mount, micro-USB charge port, ON/OFF slide switch | £22 |
| 803040 1S LiPo, 1000 mAh | 40×30×8 mm, PH2.0-2P connector. Fits under the HAT | £8 |
| microSD card, 8 GB or larger | A1 rated | £6 |
| micro-USB OTG adapter | Pi micro-B to USB-A | £3 |
| USB-A to **mini-B** cable | The Ditto+ port is mini-B. One ships with the pedal | £4 |
| | | **£61** |

Optional panel parts, £10 the lot:

| Part | Notes | ~Cost |
|---|---|---|
| Momentary tactile switch | End-session button | £1 |
| 5 mm LED and 330 Ω resistor | Status indicator | £1 |
| PiOLED 128×32 (SSD1306) | Address, progress, do-not-unplug warning | £8 |

Cable chain: Pi micro-USB-B → OTG adapter → USB-A → mini-B → Ditto+.

The pedal keeps its own 9 V supply and doesn't draw meaningfully from USB, so
the battery only has to run the Pi.

---

## Power

The UPS HAT's TPS61088 boost supplies about 1.8 A at full charge, against the
Zero 2 W's ~500 mA peak. An INA219 on I²C `0x43` reports voltage and current.

### Runtime

1000 mAh at 3.7 V is 3.7 Wh, roughly 3.1 Wh usable.

| State | Draw | Runtime |
|---|---|---|
| Idle, WiFi up | ~0.8 W | ~4 hours |
| Converting and writing | ~2.0–2.5 W | ~1.3 hours |
| A ten-minute session | ~0.3 Wh | ~10 sessions per charge |

Switch it off between sessions.

### Charge estimation

The INA219 measures voltage, not charge. `power.py` maps 3.1 V to 0% and 4.2 V
to 100% linearly and averages the last eight samples. That is crude: the cell
sags under load and reads high while charging.

Two thresholds use it:

| Threshold | Effect |
|---|---|
| Under 30% (`MIN_CHARGE_PCT`) | Writes are refused |
| At or under 15% (`CRITICAL_CHARGE_PCT`) | The session ends automatically: finish the queued writes, flush, unmount, halt |

Both are skipped if the gauge is absent or if the cell is charging. **A build
without the UPS HAT has no charge protection at all** — `can_write()` returns
true when no gauge is present, on the basis that a missing sensor shouldn't
brick the device.

The reason for the 30% floor: available current falls as the cell drains, and an
over-current condition makes the Pi reboot-loop. A reboot mid-write can corrupt
the pedal's filesystem.

---

## Optional panel

All three parts are optional. The software checks for each at startup and
carries on without them.

| | BCM | Physical pin | Wiring |
|---|---|---|---|
| Button | 17 | 11 | To GND (pin 9). Internal pull-up, no resistor |
| LED | 27 | 13 | Through ~330 Ω to GND (pin 14) |
| OLED | I²C bus 1 | 3 (SDA), 5 (SCL) | Address `0x3C` |

128×64 displays work too; set `DITTO_OLED_HEIGHT=64`.

### Settings

All are environment variables read at startup.

| Variable | Default | Purpose |
|---|---|---|
| `DITTO_BUTTON_GPIO` | `17` | Button pin, BCM numbering |
| `DITTO_LED_GPIO` | `27` | LED pin, BCM numbering |
| `DITTO_OLED_HEIGHT` | `32` | `32` or `64` |
| `DITTO_PORT` | `80` | HTTP port |
| `DITTO_PEDAL_LABEL` | `DITTOPLUS` | Volume label used to find the pedal |
| `DITTO_DATA` | `/var/lib/ditto` | Data directory |
| `DITTO_MOUNT` | `/media/ditto` | Pedal mount point |

Set them in the systemd unit, which lives on the read-only filesystem — see
[provisioning.md](provisioning.md#changing-anything-afterwards):

```ini
[Service]
Environment=DITTO_OLED_HEIGHT=64
```

### Button

| Action | Result |
|---|---|
| Short press | Wait for queued writes to finish, flush, unmount, halt |
| Hold 3 s | Skip the queue, flush, unmount, halt |

Both unmount. The difference is whether pending writes are allowed to complete.

The button is a safety control. The read-only root filesystem protects the Pi's
SD card from an abrupt power cut, but nothing protects the pedal's FAT volume.
The button gives a clean ending without needing a browser, which matters when
you're at the device with your hand on the cable.

### LED

| Pattern | Meaning |
|---|---|
| Slow blink | Booting, or running with no pedal connected |
| Steady | Pedal mounted and idle, safe to end the session |
| Fast blink | Converting or writing. Do not unplug |
| Off | Halted, safe to unplug and switch off |
| Double blink | Error, check the web UI |

---

## Assembly

Switch the HAT to OFF before connecting the battery. Waveshare warn the board
can be damaged by shorting otherwise.

A new cell may not power the board until it has charged on the HAT long enough
to activate its protection circuit.

The pogo pins can carry enough current to power the Pi while missing the signal
pins, so the HAT can appear to work while the gauge is invisible. If the Pi has
a soldered GPIO header, the joints may be holding the boards apart. Screw them
together instead of relying on pin pressure.

Verifying I²C, and the group membership the service needs, are covered in
[provisioning.md](provisioning.md#4-i2c-and-gpio-access).

---

## Enclosure

There isn't one. The stack is about 20 mm tall over a 65 × 30 mm footprint. The
HAT's charge port and slide switch both need to stay reachable, and the OTG
adapter on the Pi's data port is bulky enough that a short pigtail may fit
better than a rigid adapter.
