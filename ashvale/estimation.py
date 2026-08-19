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

"""State estimation: the layer between a noisy sensor and an honest number.

Two jobs here, both familiar from soft-sensor work:

1. `ThermalCompensator` removes the SoC self-heating bias. The classic
   Sense HAT correction `T = T_sensor - k (T_cpu - T_sensor)` is a
   one-parameter grey-box model. We keep the structure and estimate `k`
   recursively whenever a trusted reference reading is supplied, which
   beats hard-coding 1/1.5 and hoping.

2. `SignalTracker` runs a constant-velocity Kalman filter per signal.
   The filtered level is a denoised measurement; the filtered rate is the
   thing you actually want for weather. A finite difference of a 0.05 hPa
   noise floor over 5 minutes is garbage. A Kalman rate is not.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional

import numpy as np

from .physics import saturation_vapour_pressure


@dataclass
class KalmanCV:
    """Constant-velocity Kalman filter for one scalar signal.

    State x = [level, rate]. Process noise is the standard continuous
    white-noise-acceleration model, so `q` has units of (signal/s^2)^2/s
    and is the only knob that matters: raise it to track faster, lower it
    to smooth harder.
    """

    q: float
    r: float
    x: np.ndarray = field(default_factory=lambda: np.zeros(2))
    P: np.ndarray = field(default_factory=lambda: np.eye(2) * 1e3)
    initialised: bool = False
    nis: float = 0.0  # normalised innovation squared, for health monitoring
    innovation_z: float = 0.0

    def update(self, z: float, dt: float) -> tuple[float, float]:
        if not np.isfinite(z):
            return float(self.x[0]), float(self.x[1])
        if not self.initialised:
            self.x = np.array([z, 0.0])
            self.P = np.array([[self.r, 0.0], [0.0, 1e-4]])
            self.initialised = True
            return z, 0.0

        dt = float(max(min(dt, 3600.0), 1e-3))
        F = np.array([[1.0, dt], [0.0, 1.0]])
        Q = self.q * np.array([[dt ** 3 / 3.0, dt ** 2 / 2.0],
                               [dt ** 2 / 2.0, dt]])

        # predict
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + Q

        # update
        H = np.array([[1.0, 0.0]])
        y = float(z) - float((H @ self.x)[0])
        S = float((H @ self.P @ H.T)[0, 0]) + self.r
        K = (self.P @ H.T) / S
        self.x = self.x + (K.flatten() * y)
        I_KH = np.eye(2) - K @ H
        self.P = I_KH @ self.P @ I_KH.T + K @ K.T * self.r   # Joseph form, stays PSD

        self.nis = (y * y) / S
        # y/sqrt(S) is the innovation in units of its own predicted spread, so it
        # is comparable across signals and should look standard normal when the
        # filter is consistent. Cheap to keep, and the only honest way to see
        # skew or fat tails rather than inferring them from a single NIS value.
        self.innovation_z = float(y / np.sqrt(S)) if S > 0 else 0.0
        return float(self.x[0]), float(self.x[1])

    @property
    def level(self) -> float:
        return float(self.x[0])

    @property
    def rate(self) -> float:
        """Signal units per second."""
        return float(self.x[1])

    def to_dict(self) -> Dict:
        return {"q": self.q, "r": self.r, "x": self.x.tolist(),
                "P": self.P.tolist(), "initialised": self.initialised}

    def load_state(self, d: Dict) -> None:
        """Restore the estimate only, leaving q and r as configured.

        q and r are tuning, not something the filter learned. Taking them from
        the state file pins whatever values were in force when it was written,
        so editing them in config.yaml does nothing until someone thinks to
        delete the state, and nobody thinks to delete the state. That cost a
        retune here: the new q was deployed, the service restarted, and the
        filters quietly carried on with the old one.

        P may be inconsistent with a newly changed q. That is harmless: the
        filter re-converges within a few hundred samples, which is far cheaper
        than a tuning change that appears to work and does not.
        """
        self.x = np.array(d["x"], dtype=float)
        self.P = np.array(d["P"], dtype=float)
        self.initialised = bool(d["initialised"])

    @classmethod
    def from_dict(cls, d: Dict) -> "KalmanCV":
        kf = cls(q=d["q"], r=d["r"])
        kf.load_state(d)
        return kf


class ThermalCompensator:
    """Grey-box removal of SoC self-heating.

    Model: T_true = T_sensor - k * (T_cpu - T_sensor), k >= 0.

    `k` is updated by recursive least squares whenever `calibrate()` is
    called with a trusted reference temperature (a mercury thermometer, a
    second logger, or a nearby METAR reading). Until then the configured
    prior is used and clamped to a physically sane band, because a runaway
    `k` produces confident nonsense, which is worse than a mild bias.
    """

    def __init__(self, k0: float = 0.55, k_min: float = 0.15, k_max: float = 1.2,
                 forgetting: float = 0.98):
        self.k = float(k0)
        self.k_min, self.k_max = float(k_min), float(k_max)
        self.P = 10.0
        self.lam = float(forgetting)
        self.n_calibrations = 0
        self.last_residual = 0.0

    def compensate(self, t_sensor: float, t_cpu: float) -> float:
        if not (np.isfinite(t_sensor) and np.isfinite(t_cpu)):
            return float(t_sensor)
        delta = max(t_cpu - t_sensor, 0.0)
        return float(t_sensor - self.k * delta)

    def calibrate(self, t_sensor: float, t_cpu: float, t_reference: float) -> Dict:
        """One RLS step on k. Regressor is the CPU/sensor gradient."""
        phi = max(t_cpu - t_sensor, 0.0)
        target = t_sensor - t_reference          # what k*phi should equal
        denom = self.lam + phi * self.P * phi
        gain = (self.P * phi) / denom if denom > 1e-12 else 0.0
        residual = target - self.k * phi
        self.k = float(np.clip(self.k + gain * residual, self.k_min, self.k_max))
        self.P = float((self.P - gain * phi * self.P) / self.lam)
        self.P = float(np.clip(self.P, 1e-6, 1e4))
        self.n_calibrations += 1
        self.last_residual = float(residual)
        return {"k": self.k, "residual": self.last_residual, "n": self.n_calibrations}

    def to_dict(self) -> Dict:
        return {"k": self.k, "P": self.P, "lam": self.lam, "k_min": self.k_min,
                "k_max": self.k_max, "n": self.n_calibrations}

    @classmethod
    def from_dict(cls, d: Dict) -> "ThermalCompensator":
        tc = cls(d["k"], d["k_min"], d["k_max"], d["lam"])
        tc.P = d["P"]
        tc.n_calibrations = d.get("n", 0)
        return tc


class HumidityCompensator:
    """Corrects relative humidity for a sensor sitting hotter than the air.

    The HTS221 reports RH at its own temperature, but the air you care about is
    at the compensated temperature. Vapour pressure is what is conserved between
    the two, so

        RH_true = RH_sensor * es(T_sensor) / es(T_true)

    Because the element runs hot, es(T_sensor) > es(T_true) and an uncorrected
    reading is biased low, by several points on a warm board. That term needs no
    calibration constant at all: it falls out of the thermal compensation that is
    already running.

    Measured on real hardware the psychrometric term is the wrong model: against
    a reference hygrometer reading 50.4%, this board's HTS221 reported 75.4%, so
    it reads HIGH where the thermal argument predicts LOW. The dominant error is
    an additive element bias, which is what `offset` corrects, estimated from a
    trusted reference by the same one-step RLS used for `k` with the regressor
    fixed at 1, so repeated calibrations converge to a weighted mean. The
    psychrometric term is therefore off by default and gated on config.

    How it fails: calibrate against a reference while the board is cool, then let
    CPU load rise, and an offset-only correction drifts because the psychrometric
    error grows with the gradient. Applying the vapour-pressure term first is
    exactly what keeps `offset` a constant rather than a function of CPU load.
    Clamped for the same reason `k` is: one mistyped reference otherwise biases
    every reading until you notice.
    """

    def __init__(self, offset: float = 0.0, off_min: float = -35.0,
                 off_max: float = 35.0, forgetting: float = 0.98,
                 psychrometric: bool = False):
        self.psychrometric = bool(psychrometric)
        self.offset = float(offset)
        self.off_min, self.off_max = float(off_min), float(off_max)
        self.P = 10.0
        self.lam = float(forgetting)
        self.n_calibrations = 0
        self.last_residual = 0.0

    def _psychrometric(self, rh_sensor: float, t_sensor: float, t_true: float) -> float:
        if not self.psychrometric:
            return float(rh_sensor)
        es_s = float(saturation_vapour_pressure(t_sensor))
        es_t = float(saturation_vapour_pressure(t_true))
        if not np.isfinite(es_s) or not np.isfinite(es_t) or es_t <= 1e-9:
            return float(rh_sensor)
        return float(rh_sensor * es_s / es_t)

    def compensate(self, rh_sensor: float, t_sensor: float, t_true: float) -> float:
        if not (np.isfinite(rh_sensor) and np.isfinite(t_sensor) and np.isfinite(t_true)):
            return float(rh_sensor)
        base = self._psychrometric(rh_sensor, t_sensor, t_true)
        return float(np.clip(base + self.offset, 0.0, 100.0))

    def calibrate(self, rh_sensor: float, t_sensor: float, t_true: float,
                  rh_reference: float) -> Dict:
        """One RLS step on the residual offset. Regressor is 1."""
        base = self._psychrometric(rh_sensor, t_sensor, t_true)
        target = float(rh_reference) - base
        denom = self.lam + self.P
        gain = self.P / denom if denom > 1e-12 else 0.0
        residual = target - self.offset
        self.offset = float(np.clip(self.offset + gain * residual,
                                    self.off_min, self.off_max))
        self.P = float(np.clip((self.P - gain * self.P) / self.lam, 1e-6, 1e4))
        self.n_calibrations += 1
        self.last_residual = float(residual)
        return {"offset": self.offset, "residual": self.last_residual,
                "n": self.n_calibrations, "psychrometric": base - float(rh_sensor)}

    def to_dict(self) -> Dict:
        return {"offset": self.offset, "P": self.P, "lam": self.lam,
                "off_min": self.off_min, "off_max": self.off_max,
                "n": self.n_calibrations, "psychrometric": self.psychrometric}

    @classmethod
    def from_dict(cls, d: Dict) -> "HumidityCompensator":
        hc = cls(d["offset"], d["off_min"], d["off_max"], d["lam"],
                 d.get("psychrometric", False))
        hc.P = d["P"]
        hc.n_calibrations = d.get("n", 0)
        return hc


class SignalTracker:
    """Bank of Kalman filters plus the compensators, driven at sample rate."""

    def __init__(self, cfg):
        self.compensator = ThermalCompensator(
            cfg.sensor.cpu_heat_k, cfg.sensor.cpu_heat_k_min,
            cfg.sensor.cpu_heat_k_max,
        )
        self.hum_compensator = HumidityCompensator(
            cfg.sensor.hum_offset, cfg.sensor.hum_offset_min,
            cfg.sensor.hum_offset_max,
            psychrometric=cfg.sensor.hum_psychrometric,
        )
        self.filters = {
            "temperature": KalmanCV(cfg.sensor.kalman_q_temp, cfg.sensor.kalman_r_temp),
            "humidity": KalmanCV(cfg.sensor.kalman_q_hum, cfg.sensor.kalman_r_hum),
            "pressure": KalmanCV(cfg.sensor.kalman_q_press, cfg.sensor.kalman_r_press),
        }
        self.last_ts: Optional[float] = None
        # 600 samples per signal is 20 minutes at the live 2 s cadence, about
        # 14 kB total. Bounded on purpose: this board has 512 MB and an
        # unbounded diagnostic buffer is a slow memory leak with a nice name.
        self.innovations: Dict[str, deque] = {
            k: deque(maxlen=600) for k in self.filters
        }

    def step(self, ts: float, temp_raw: float, hum: float, press: float,
             cpu_temp: float) -> Dict[str, float]:
        dt = (ts - self.last_ts) if self.last_ts is not None else 1.0
        self.last_ts = ts

        temp_c = self.compensator.compensate(temp_raw, cpu_temp)
        # RH is reported at the element's temperature, not the air's, so it must
        # be moved onto the compensated temperature before it is filtered.
        hum_c = self.hum_compensator.compensate(hum, temp_raw, temp_c)
        t_lvl, t_rate = self.filters["temperature"].update(temp_c, dt)
        h_lvl, h_rate = self.filters["humidity"].update(hum_c, dt)
        p_lvl, p_rate = self.filters["pressure"].update(press, dt)
        for name, kf in self.filters.items():
            if kf.initialised:
                self.innovations[name].append(kf.innovation_z)

        return {
            "temp_c": temp_c,
            "hum_c": hum_c,
            "temp_smooth": t_lvl,
            "temp_rate": t_rate * 3600.0,      # C per hour
            "hum_smooth": h_lvl,
            "hum_rate": h_rate * 3600.0,       # % per hour
            "press_smooth": p_lvl,
            "press_rate": p_rate * 3600.0,     # hPa per hour, the forecaster's gold
            "nis_temp": self.filters["temperature"].nis,
            "nis_press": self.filters["pressure"].nis,
        }

    def to_dict(self) -> Dict:
        return {
            "compensator": self.compensator.to_dict(),
            "hum_compensator": self.hum_compensator.to_dict(),
            "filters": {k: v.to_dict() for k, v in self.filters.items()},
            "last_ts": self.last_ts,
        }

    def load_dict(self, d: Dict) -> None:
        self.compensator = ThermalCompensator.from_dict(d["compensator"])
        # Absent from state files written before humidity compensation existed.
        if d.get("hum_compensator"):
            self.hum_compensator = HumidityCompensator.from_dict(d["hum_compensator"])
        # Deliberately not KalmanCV.from_dict here: that would restore the
        # persisted q and r over the configured ones. Only the estimate is
        # restored, and only for filters this build still has.
        for name, saved in d.get("filters", {}).items():
            kf = self.filters.get(name)
            if kf is not None:
                kf.load_state(saved)
        self.last_ts = d.get("last_ts")


def stuck_sensor_score(values: np.ndarray, window: int = 60) -> float:
    """Fraction of the last `window` samples that are bit-identical.

    An HTS221 that latches is the quietest failure mode there is: the
    dashboard looks perfect, the model trains happily, and every forecast
    is confidently wrong. This is the cheapest possible smoke alarm.
    """
    if values.size < 5:
        return 0.0
    tail = values[-window:]
    tail = tail[np.isfinite(tail)]
    if tail.size < 5:
        return 0.0
    return float(np.mean(np.abs(np.diff(tail)) < 1e-9))
