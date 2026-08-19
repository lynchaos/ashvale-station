# Copyright 2026 Kemal Yaylali
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Hardware access, with a simulator so the suite runs on your laptop too.

`SenseBoard` is the only place that touches `sense_hat` or `smbus2`. If
either import fails (which it will on any machine that is not a Pi), the
board falls back to `SimulatedBoard`: a small stochastic-differential
weather model that produces plausible diurnal cycles, synoptic pressure
waves and sensor noise. Train on it, develop against it, then move the
same code to the Pi unchanged.
"""

from __future__ import annotations

import logging
import math
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .physics import dew_point, sea_level_pressure, solar_position

log = logging.getLogger(__name__)

TCS3400_ENABLE = 0x80
TCS3400_ATIME = 0x81
TCS3400_CONTROL = 0x8F
TCS3400_CDATA = 0x94


def read_throttled() -> Optional[Dict[str, Any]]:
    """Raspberry Pi undervoltage and throttling flags, or None if all clear.

    Bit 0 is undervoltage now, 16 is undervoltage since boot, 2 is arm
    frequency capped, 3 is thermal throttling. A capped or browning-out board
    runs its SoC at a different temperature, and the SoC temperature is the
    regressor in the self-heating compensation, so the visible symptom is a
    temperature bias with no apparent cause.
    """
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if "=" not in out:
        return None
    try:
        bits = int(out.split("=", 1)[1], 0)
    except ValueError:
        return None
    if bits == 0:
        return None
    now = {0: "undervoltage", 1: "arm_capped", 2: "throttled", 3: "soft_temp_limit"}
    ever = {16: "undervoltage_since_boot", 17: "arm_capped_since_boot",
            18: "throttled_since_boot", 19: "soft_temp_limit_since_boot"}
    active = [name for bit, name in now.items() if bits & (1 << bit)]
    historic = [name for bit, name in ever.items() if bits & (1 << bit)]
    return {"raw": hex(bits), "active": active, "since_boot": historic,
            "severity": "warn" if active else "info"}


def read_cpu_temperature() -> float:
    """Core temperature in C. This is the single most important nuisance
    variable on a Sense HAT: the HTS221 and LPS25HB sit millimetres above a
    SoC that runs 30 C hotter than the room."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as fh:
            return float(fh.read().strip()) / 1000.0
    except Exception:
        return float("nan")


# Per-chip thermal coupling to the SoC, and per-chip noise.
#
# The Sense HAT carries two independent thermometers at different distances
# from the SoC, and they are not equally good. Measured over 12 samples on a
# real board: HTS221 30.973 C at sd 0.060, LPS25HB 29.810 C at sd 0.443, a
# standing gradient of 1.163 C with the SoC at 44.55 C.
#
# These two couplings are chosen so their forward models average to exactly the
# k = 0.55 the compensator is tuned against. The aggregate behaviour is
# therefore unchanged and only the per-channel detail is new, which matters
# because that gradient is a second observation of self-heating.
K_HTS221, K_LPS25HB = 0.6164, 0.4889
SD_HTS221, SD_LPS25HB = 0.049, 0.007


class _ChannelNoise:
    """Running white-noise variance of one thermometer.

    Taken from the first difference rather than a windowed variance. Over one
    2 s sample the air moves far less than either chip's own jitter, so
    var(diff)/2 is the noise and is blind to the weather underneath it. A
    windowed variance would measure the weather instead and would rise, not
    fall, on a calm day.
    """

    def __init__(self, prior_sd: float, lam: float = 0.995, warmup: int = 200):
        self.var = float(prior_sd) ** 2
        self.prior = self.var
        self.lam = float(lam)
        self.warmup = int(warmup)
        self.last: Optional[float] = None
        self.n = 0

    def update(self, value: float) -> float:
        if not math.isfinite(value):
            return max(self.var, 1e-8)
        if self.last is not None:
            d = value - self.last
            self.var = self.lam * self.var + (1.0 - self.lam) * (d * d / 2.0)
            self.n += 1
        self.last = value
        if self.n < self.warmup:
            # Blend toward the prior while the estimate is young, so one quiet
            # minute cannot hand a channel 100% of the weight on no evidence.
            w = self.n / float(self.warmup)
            return max(w * self.var + (1.0 - w) * self.prior, 1e-8)
        return max(self.var, 1e-8)


class SimulatedBoard:
    """Ornstein-Uhlenbeck weather with a diurnal driver. Good enough to
    exercise every code path and to sanity-check a model's skill score."""

    def __init__(self, latitude: float = 52.2, longitude: float = 0.12, seed: int = 7):
        self.rng = np.random.default_rng(seed)
        self.lat, self.lon = latitude, longitude
        self.t0 = time.time()
        self.press_anom = 0.0
        self.temp_anom = 0.0
        self.hum_anom = 0.0
        self.last = self.t0
        self.available = False

    def _step(self, now: float) -> None:
        dt = max(min(now - self.last, 600.0), 0.0)
        self.last = now
        # synoptic pressure: slow OU process, tau ~ 30 h, sigma ~ 9 hPa
        self.press_anom += (-self.press_anom / (30 * 3600) * dt
                            + 9.0 * math.sqrt(2 * dt / (30 * 3600)) * self.rng.normal())
        self.temp_anom += (-self.temp_anom / (6 * 3600) * dt
                           + 1.8 * math.sqrt(2 * dt / (6 * 3600)) * self.rng.normal())
        self.hum_anom += (-self.hum_anom / (4 * 3600) * dt
                          + 6.0 * math.sqrt(2 * dt / (4 * 3600)) * self.rng.normal())

    def read(self) -> Dict[str, Any]:
        now = time.time()
        self._step(now)
        elev, _ = solar_position(now, self.lat, self.lon)
        doy = time.gmtime(now).tm_yday
        seasonal = 6.5 * math.sin(2 * math.pi * (doy - 105) / 365.25)
        solar_gain = 5.0 * max(elev, 0.0) / 60.0
        temp = 12.0 + seasonal + solar_gain + self.temp_anom
        rh = float(np.clip(78.0 - 1.9 * (temp - 12.0) + self.hum_anom, 12.0, 99.0))
        press = 1013.0 + self.press_anom
        lux = max(0.0, 60000.0 * max(math.sin(math.radians(max(elev, 0.0))), 0.0)) + 8.0
        cpu = temp + 22.0 + 1.5 * self.rng.normal()
        # forward model must invert the compensator exactly, see scripts/simulate.py
        t_h = (temp + K_HTS221 * cpu) / (1.0 + K_HTS221) + SD_HTS221 * self.rng.normal()
        t_p = (temp + K_LPS25HB * cpu) / (1.0 + K_LPS25HB) + SD_LPS25HB * self.rng.normal()
        return {
            "temp_raw": (t_h + t_p) / 2.0,
            "temp_h": t_h,
            "temp_p": t_p,
            "hum": rh + 0.4 * self.rng.normal(),
            "press": press + 0.05 * self.rng.normal(),
            "cpu_temp": cpu,
            "lux": lux * (0.35 + 0.65 * self.rng.random()),
            "r": int(lux * 0.30), "g": int(lux * 0.34), "b": int(lux * 0.28),
            "pitch": 0.4 * self.rng.normal(), "roll": 0.4 * self.rng.normal(),
            "yaw": 180.0 + self.rng.normal(), "compass": 180.0 + 2 * self.rng.normal(),
            "ax": 0.0, "ay": 0.0, "az": 1.0,
            "gx": 0.0, "gy": 0.0, "gz": 0.0,
        }

    def clear(self, *_a, **_k):  # LED no-op
        pass


class OutdoorProbe:
    """Optional DS18B20 on the 1-Wire bus, read through the kernel's w1 driver.

    Why this matters more than any model change: indoors the station forecasts
    a room. Pressure passes through walls, temperature and humidity do not. One
    three-pound sensor on a metre of cable outside the window removes the single
    largest caveat in the project.

    No new dependency. The kernel exposes each probe as a text file under
    /sys/bus/w1/devices/28-*/w1_slave, so this is a file read and two string
    splits. Enable with `dtoverlay=w1-gpio` in /boot/firmware/config.txt.

    How it fails: the DS18B20 takes up to 750 ms to convert, and the driver
    blocks for that whole time. Reading it on the 2 s sample loop would eat a
    third of the budget on a single-issue core, so it is polled on its own
    slower cadence and the last good value is reused in between. A probe that
    goes missing (cable pulled, bad CRC) returns None rather than a stale value
    forever: `age_s` lets the caller decide when to stop trusting it.
    """

    ROOT = "/sys/bus/w1/devices"

    def __init__(self, min_period_s: float = 20.0) -> None:
        self.min_period_s = float(min_period_s)
        self.device: Optional[str] = None
        self.available = False
        self.last_value: Optional[float] = None
        self.last_ts: Optional[float] = None
        self.errors = 0
        self._discover()

    def _discover(self) -> None:
        try:
            root = Path(self.ROOT)
            if not root.is_dir():
                return
            probes = sorted(p for p in root.glob("28-*") if (p / "w1_slave").exists())
            if probes:
                self.device = str(probes[0] / "w1_slave")
                self.available = True
                log.info("outdoor probe found at %s", self.device)
        except OSError as exc:
            log.warning("1-wire scan failed: %r", exc)

    def read(self) -> Optional[float]:
        """Celsius, or None. Cached between polls so the sample loop never blocks."""
        if not self.available or self.device is None:
            return None
        now = time.time()
        if self.last_ts is not None and (now - self.last_ts) < self.min_period_s:
            return self.last_value
        try:
            with open(self.device, "r") as fh:
                text = fh.read()
        except OSError as exc:
            self.errors += 1
            log.warning("outdoor probe read failed: %r", exc)
            return self.last_value
        # Two lines: the first ends in YES only when the CRC checked out.
        if "YES" not in text.split("\n")[0]:
            self.errors += 1
            return self.last_value
        marker = text.find("t=")
        if marker < 0:
            self.errors += 1
            return self.last_value
        try:
            milli = int(text[marker + 2:].strip())
        except ValueError:
            self.errors += 1
            return self.last_value
        # 85000 is the DS18B20 power-on default and means "never converted".
        if milli == 85000:
            self.errors += 1
            return self.last_value
        value = milli / 1000.0
        if not (-55.0 <= value <= 125.0):
            self.errors += 1
            return self.last_value
        self.last_value = value
        self.last_ts = now
        return value

    def status(self) -> Dict[str, Any]:
        age = None if self.last_ts is None else round(time.time() - self.last_ts, 1)
        return {"available": self.available, "device": self.device,
                "value_c": self.last_value, "age_s": age, "errors": self.errors}


class SenseBoard:
    """Real hardware wrapper. Attribute `available` tells you which world
    you are in without try/except at every call site."""

    def __init__(self, rotation: int = 90, low_light: bool = True,
                 tcs_addr: int = 0x39, latitude: float = 52.2, longitude: float = 0.12):
        self.available = False
        self.has_colour = False
        self.sense = None
        self.bus = None
        self.tcs_addr = tcs_addr
        self._sim = SimulatedBoard(latitude, longitude)
        self._noise_h = _ChannelNoise(SD_HTS221)
        self._noise_p = _ChannelNoise(SD_LPS25HB)
        # Slow EWMA of the standing gradient between the two chips. About a
        # 10-minute time constant at the 2 s cadence: long enough to ignore
        # per-sample noise, short enough to follow a real change in SoC load.
        self._gradient: Optional[float] = None
        self._gradient_lam = 0.9967

        try:
            from sense_hat import SenseHat  # type: ignore
            self.sense = SenseHat()
            self.sense.low_light = low_light
            self.sense.set_rotation(rotation)
            self.available = True
        except Exception:
            self.sense = None

        if self.available:
            try:
                import smbus2  # type: ignore
                self.bus = smbus2.SMBus(1)
                self.bus.write_byte_data(self.tcs_addr, TCS3400_ENABLE, 0x03)   # power + RGBC
                self.bus.write_byte_data(self.tcs_addr, TCS3400_ATIME, 0xD5)    # 100 ms
                self.bus.write_byte_data(self.tcs_addr, TCS3400_CONTROL, 0x00)  # 1x gain
                self.has_colour = True
            except Exception:
                self.has_colour = False

    # ---------------------------------------------------------------- IO

    def colour(self) -> Dict[str, Any]:
        if not self.has_colour:
            return {"clear": 0, "red": 0, "green": 0, "blue": 0, "hex": "#334155", "cct": None}
        try:
            data = self.bus.read_i2c_block_data(self.tcs_addr, TCS3400_CDATA | 0x80, 8)
            c = data[0] | (data[1] << 8)
            r = data[2] | (data[3] << 8)
            g = data[4] | (data[5] << 8)
            b = data[6] | (data[7] << 8)
            return _colour_payload(c, r, g, b)
        except Exception:
            return {"clear": 0, "red": 0, "green": 0, "blue": 0, "hex": "#334155", "cct": None}

    def _fuse(self, t_h: float, t_p: float) -> tuple[float, float]:
        """Combine the two thermometers by inverse variance.

        A plain average of a quiet sensor and a noisy one throws the quiet one
        away. Measured on the board at 0.5 s: the LPS25HB carries a white-noise
        sd of 0.007 C against the HTS221's 0.049 C, so optimal weighting is
        about 98/2 and cuts the raw noise by roughly 3.7x.

        The trap is that the two chips do not agree. They sit at different
        distances from the SoC and stand about 1.3 C apart, so weighting them
        by variance would drag temp_raw most of the way onto the LPS25HB and
        shift it by more than half a degree. The compensator's k was fitted
        against the mean of the two, and after the 1.55x gain of the inverse
        model that is a full degree of silent bias on every reading and every
        forecast built from it.

        So the gradient is tracked and removed before weighting, and only the
        deviations are fused. The mean is left exactly where the average put
        it, k stays valid, and the noise still falls. The gradient itself is
        kept because it is a second observation of self-heating and is what
        would let k be identified without a reference thermometer.
        """
        if not (math.isfinite(t_h) and math.isfinite(t_p)):
            good = [v for v in (t_h, t_p) if math.isfinite(v)]
            return (good[0] if good else float("nan")), float("nan")

        var_h = self._noise_h.update(t_h)
        var_p = self._noise_p.update(t_p)

        gap = t_h - t_p
        if self._gradient is None:
            self._gradient = gap
        else:
            lam = self._gradient_lam
            self._gradient = lam * self._gradient + (1.0 - lam) * gap

        # Centre both channels on what the plain average would have reported.
        half = self._gradient / 2.0
        w_h, w_p = 1.0 / var_h, 1.0 / var_p
        fused = (w_h * (t_h - half) + w_p * (t_p + half)) / (w_h + w_p)
        return float(fused), float(1.0 / (w_h + w_p))

    def read(self) -> Dict[str, Any]:
        """One full multi-sensor sample. Raw, uncompensated, untouched."""
        if not self.available:
            row = self._sim.read()
            col = _colour_payload(int(row["lux"]), row["r"], row["g"], row["b"])
            row.update({"lux": col["clear"], "r": col["red"], "g": col["green"],
                        "b": col["blue"], "colour": col, "simulated": True})
            return row

        s = self.sense
        t_h = s.get_temperature_from_humidity()
        t_p = s.get_temperature_from_pressure()
        temp_raw, temp_var = self._fuse(t_h, t_p)
        orientation = s.get_orientation_degrees()
        accel = s.get_accelerometer_raw()
        gyro = s.get_gyroscope_raw()
        col = self.colour()

        def wrap(v):
            return v - 360.0 if v > 180.0 else v

        return {
            "temp_raw": temp_raw,
            "temp_var": temp_var,
            "temp_h": t_h,
            "temp_p": t_p,
            "hum": s.get_humidity(),
            "press": s.get_pressure(),
            "cpu_temp": read_cpu_temperature(),
            "lux": col["clear"], "r": col["red"], "g": col["green"], "b": col["blue"],
            "colour": col,
            "pitch": wrap(orientation["pitch"]),
            "roll": wrap(orientation["roll"]),
            "yaw": orientation["yaw"],
            "compass": s.get_compass(),
            "ax": accel["x"], "ay": accel["y"], "az": accel["z"],
            "gx": gyro["x"], "gy": gyro["y"], "gz": gyro["z"],
            "simulated": False,
        }

    def stick_events(self) -> List[Tuple[str, str]]:
        """Pending joystick events as (direction, action), oldest first.

        Non-blocking, and returns [] when nothing has happened. The library
        buffers events, so polling slowly loses none of them.
        """
        if self.sense is None:
            return []
        try:
            return [(e.direction, e.action) for e in self.sense.stick.get_events()]
        except Exception:
            return []

    # --------------------------------------------------------------- LED

    def clear(self, *args):
        if self.sense is not None:
            self.sense.clear(*args)

    def show_message(self, text: str, scroll_speed: float = 0.065, text_colour=None):
        if self.sense is not None:
            self.sense.show_message(text, scroll_speed=scroll_speed,
                                    text_colour=text_colour or [255, 255, 255])

    def set_pixels(self, pixels):
        if self.sense is not None:
            self.sense.set_pixels(pixels)


def _colour_payload(c: int, r: int, g: int, b: int) -> Dict[str, Any]:
    denom = max(int(c), 1)
    nr = min(int((r / denom) * 255), 255)
    ng = min(int((g / denom) * 255), 255)
    nb = min(int((b / denom) * 255), 255)
    return {
        "clear": int(c), "red": int(r), "green": int(g), "blue": int(b),
        "hex": f"#{nr:02x}{ng:02x}{nb:02x}",
        "cct": correlated_colour_temperature(r, g, b),
    }


def correlated_colour_temperature(r: float, g: float, b: float) -> Optional[float]:
    """McCamy's approximation, in kelvin. Distinguishes a tungsten desk lamp
    (~2700 K) from overcast daylight (~6500 K), which turns the colour sensor
    into a crude `is anyone home` and `is it cloudy` detector."""
    if (r + g + b) <= 0:
        return None
    X = -0.14282 * r + 1.54924 * g + -0.95641 * b
    Y = -0.32466 * r + 1.57837 * g + -0.73191 * b
    Z = -0.68202 * r + 0.77073 * g + 0.56332 * b
    denom = X + Y + Z
    if abs(denom) < 1e-9:
        return None
    x, y = X / denom, Y / denom
    if abs(y - 0.1858) < 1e-9:
        return None
    n = (x - 0.3320) / (0.1858 - y)
    cct = 449 * n ** 3 + 3525 * n ** 2 + 6823.3 * n + 5520.33
    return float(cct) if 800 < cct < 25000 else None


def enrich(raw: Dict[str, Any], altitude_m: float) -> Dict[str, Any]:
    """Add derived quantities that do not need any model state."""
    out = dict(raw)
    temp = raw.get("temp_raw", float("nan"))
    hum = raw.get("hum", float("nan"))
    press = raw.get("press", float("nan"))
    out["dew_c"] = float(dew_point(temp, hum))
    out["press_slp"] = float(sea_level_pressure(press, temp, altitude_m))
    return out
