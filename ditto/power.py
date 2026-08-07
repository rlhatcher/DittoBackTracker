"""Battery monitoring via the UPS HAT's INA219.

The gauge is voltage-derived, not coulomb counting: the cell sags under load
and over-reads while charging. So readings are smoothed and thresholds are
set conservatively. Absent hardware, everything degrades to "unknown" and the
charge checks pass — a missing gauge must not brick the device.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Optional

from . import config

_SHUNT_OHMS = 0.1
_FULL_V = 4.2
_EMPTY_V = 3.1
_CHARGE_DEADBAND_MA = 20.0   # below this, treat the cell as not charging


class Battery:
    def __init__(self) -> None:
        self._bus = None
        self._samples: deque = deque(maxlen=8)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.available = False
        self._try_open()

    def _try_open(self) -> None:
        bus = None
        try:
            import smbus                              # type: ignore
            bus = smbus.SMBus(config.I2C_BUS)
            bus.read_i2c_block_data(config.INA219_ADDR, 0x02, 2)
            self._bus = bus
            self.available = True
        except Exception:
            # The probe can fail after the bus opened. This is retried on a
            # backoff forever, so dropping the handle would leak an fd a time.
            if bus is not None:
                try:
                    bus.close()
                except Exception:
                    pass
            self._bus = None
            self.available = False

    def _read(self, reg: int) -> int:
        d = self._bus.read_i2c_block_data(config.INA219_ADDR, reg, 2)
        v = (d[0] << 8) | d[1]
        return v - 65536 if v > 32767 else v

    def sample(self) -> None:
        if not self._bus:
            return
        try:
            bus_v = (self._read(0x02) >> 3) * 0.004      # 4 mV/LSB
            shunt_mv = self._read(0x01) * 0.01           # 10 uV/LSB
            current_ma = shunt_mv / _SHUNT_OHMS
            with self._lock:
                self._samples.append((bus_v, current_ma))
        except Exception:
            self.available = False
            self._bus = None

    @property
    def volts(self) -> Optional[float]:
        with self._lock:
            if not self._samples:
                return None
            return sum(s[0] for s in self._samples) / len(self._samples)

    @property
    def current_ma(self) -> Optional[float]:
        with self._lock:
            if not self._samples:
                return None
            return sum(s[1] for s in self._samples) / len(self._samples)

    @property
    def charging(self) -> Optional[bool]:
        # Deadband, not a bare `> 0`: the shunt reads a few mA of noise at
        # rest, and a false "charging" is the dangerous direction — it lets
        # can_write() through and stops critical() ever firing.
        c = self.current_ma
        return None if c is None else c > _CHARGE_DEADBAND_MA

    @property
    def percent(self) -> Optional[int]:
        v = self.volts
        if v is None:
            return None
        pct = (v - _EMPTY_V) / (_FULL_V - _EMPTY_V) * 100
        return max(0, min(100, int(round(pct))))

    def can_write(self) -> bool:
        """Refuse writes on a low cell — a reboot mid-write risks the pedal.
        Unknown charge, or charging, both permit writes."""
        if not self.available:
            return True
        if self.charging:
            return True
        pct = self.percent
        return pct is None or pct >= config.MIN_CHARGE_PCT

    def critical(self) -> bool:
        if not self.available or self.charging:
            return False
        pct = self.percent
        return pct is not None and pct <= config.CRITICAL_CHARGE_PCT

    def start(self) -> None:
        """Sample every 2 s. If no gauge is present, back off the reopen
        attempts to 60 s so a build without the HAT doesn't burn CPU."""
        def loop():
            retry_in = 2.0
            while not self._stop.is_set():
                if self.available:
                    self.sample()
                    retry_in = 2.0
                    self._stop.wait(2.0)
                else:
                    self._try_open()
                    if self.available:
                        continue
                    self._stop.wait(retry_in)
                    retry_in = min(retry_in * 2, 60.0)
        self._thread = threading.Thread(target=loop, daemon=True,
                                        name="battery")
        self._thread.start()

    def close(self) -> None:
        """Stop sampling and release the I2C bus."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        bus, self._bus = self._bus, None
        self.available = False
        if bus is not None:
            try:
                bus.close()
            except Exception:
                pass
