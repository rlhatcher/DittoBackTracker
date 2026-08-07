"""Physical panel: button, LED, OLED. Every piece is optional.

The LED is the only signal the user is asked to obey, so it is driven
directly from write state rather than inferred.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Callable, List, Optional

from . import config
from .oled import Oled

# LED patterns: (on_secs, off_secs) or None for steady
PATTERNS = {
    "boot":    (0.5, 0.5),
    "idle":    None,          # steady on
    "writing": (0.1, 0.1),    # fast — do not unplug
    "error":   (0.1, 0.1, 0.1, 0.6),
    "off":     (),
}


def local_ip() -> str:
    """Best-effort local address, for the display. No traffic is sent."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "no network"


class Led:
    def __init__(self, gpio: int) -> None:
        self._dev = None
        self.available = False
        try:
            from gpiozero import LED as GpioLed       # type: ignore
            self._dev = GpioLed(gpio)
            self.available = True
        except Exception:
            pass
        self._state = "boot"
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="led").start()

    def set(self, state: str) -> None:
        self._state = state

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.available:
                time.sleep(0.2)
                continue
            current = self._state
            pattern = PATTERNS.get(current)
            if pattern is None:                 # steady on
                self._dev.on()
                time.sleep(0.2)
            elif pattern == ():                 # off
                self._dev.off()
                time.sleep(0.2)
            else:
                for i, dur in enumerate(pattern):
                    if self._state != current:  # abandon a stale pattern
                        break
                    if i % 2 == 0:
                        self._dev.on()
                    else:
                        self._dev.off()
                    time.sleep(dur)

    def close(self) -> None:
        self._stop.set()
        if self.available:
            try:
                self._dev.off()
                self._dev.close()
            except Exception:
                pass


class Button:
    def __init__(self, gpio: int, on_press: Callable[[], None],
                 on_long: Optional[Callable[[], None]] = None) -> None:
        self.available = False
        self._dev = None
        try:
            from gpiozero import Button as GpioButton  # type: ignore
            self._dev = GpioButton(gpio, pull_up=True, bounce_time=0.05,
                                   hold_time=config.LONG_PRESS_SECS)
            self._held = False

            def _released():
                if self._held:
                    self._held = False
                    return
                on_press()

            def _held_cb():
                self._held = True
                if on_long:
                    on_long()

            self._dev.when_released = _released
            self._dev.when_held = _held_cb
            self.available = True
        except Exception:
            pass

    def close(self) -> None:
        if self.available:
            try:
                self._dev.close()
            except Exception:
                pass


class Panel:
    def __init__(self, on_press: Callable[[], None],
                 on_long: Optional[Callable[[], None]] = None,
                 oled_height: Optional[int] = None) -> None:
        self.led = Led(config.LED_GPIO)
        self.button = Button(config.BUTTON_GPIO, on_press, on_long)
        self.oled = Oled(height=oled_height or config.OLED_HEIGHT)

    def status(self) -> dict:
        return {"led": self.led.available,
                "button": self.button.available,
                "oled": self.oled.available}

    def render(self, state: str, lines: List[str],
               bar: Optional[float] = None) -> None:
        self.led.set(state)
        self.oled.show(lines, bar)

    def shutdown_message(self) -> None:
        self.led.set("off")
        self.oled.show(["", "  SAFE TO UNPLUG", ""])

    def close(self) -> None:
        self.led.close()
        self.button.close()
        self.oled.off()
