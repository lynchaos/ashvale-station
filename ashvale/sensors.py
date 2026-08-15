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

import math
import time
from typing import Any, Dict, Optional

import numpy as np

from .physics import dew_point, sea_level_pressure, solar_position

TCS3400_ENABLE = 0x80
TCS3400_ATIME = 0x81
TCS3400_CONTROL = 0x8F
TCS3400_CDATA = 0x94


def read_cpu_temperature() -> float:
    """Core temperature in C. This is the single most important nuisance
    variable on a Sense HAT: the HTS221 and LPS25HB sit millimetres above a
    SoC that runs 30 C hotter than the room."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as fh:
            return float(fh.read().strip()) / 1000.0
    except Exception:
        return float("nan")


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
        k_true = 0.55
        return {
            "temp_raw": (temp + k_true * cpu) / (1.0 + k_true) + 0.05 * self.rng.normal(),
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
        orientation = s.get_orientation_degrees()
        accel = s.get_accelerometer_raw()
        gyro = s.get_gyroscope_raw()
        col = self.colour()

        def wrap(v):
            return v - 360.0 if v > 180.0 else v

        return {
            "temp_raw": (t_h + t_p) / 2.0,
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
