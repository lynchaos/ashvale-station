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

"""Anomaly and drift monitoring: the part that keeps the rest honest.

Three independent detectors, because they fail in different ways:

`MahalanobisEWMA`  Multivariate novelty on the residual from a slowly
                   updated mean and shrinkage covariance. Catches a
                   window opening, a heater cycling, or a genuine squall.

`PageHinkley`      Sequential change-point detection on model error.
                   Catches the slow stuff: a sensor drifting, a season
                   turning, a model quietly going stale. This is the
                   detector that tells you *when to retrain*, which is a
                   far better trigger than a cron schedule.

`SensorHealth`     Latched values, out-of-range readings, and Kalman
                   innovation inflation. A stuck sensor is invisible to
                   the other two because it looks perfectly normal.

Shrinkage on the covariance is not optional here. With 6 signals and a
1000-sample window the sample covariance is fine, but during the first
hour it is singular, and a singular covariance turns Mahalanobis
distance into a random number generator with an authoritative name.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Dict, List, Optional

import numpy as np

SIGNALS = ["temp_c", "hum", "press_slp", "temp_rate", "press_rate", "dew_c"]


class MahalanobisEWMA:
    def __init__(self, n_dims: int, lam: float = 0.15, threshold: float = 12.0,
                 shrinkage: float = 0.15, warmup: int = 60):
        self.d = int(n_dims)
        self.lam = float(lam)
        self.threshold = float(threshold)
        self.shrinkage = float(shrinkage)
        self.warmup = int(warmup)
        self.mean = np.zeros(self.d)
        self.cov = np.eye(self.d)
        self.z = np.zeros(self.d)     # EWMA of standardised residual
        self.n = 0
        self.last_d2 = 0.0

    def update(self, x: np.ndarray) -> Dict:
        x = np.asarray(x, dtype=float).ravel()
        if x.size != self.d or not np.all(np.isfinite(x)):
            return {"d2": self.last_d2, "alarm": False, "warm": self.n < self.warmup}

        self.n += 1
        if self.n == 1:
            self.mean = x.copy()
            return {"d2": 0.0, "alarm": False, "warm": True}

        a = 1.0 / min(self.n, 500)     # slow adaptation once warm
        delta = x - self.mean
        self.mean += a * delta
        self.cov = (1 - a) * self.cov + a * np.outer(delta, delta)

        # Ledoit-Wolf style shrinkage toward a scaled identity
        target = np.eye(self.d) * (np.trace(self.cov) / self.d + 1e-9)
        cov = (1 - self.shrinkage) * self.cov + self.shrinkage * target

        try:
            resid = np.linalg.solve(cov, delta)
        except np.linalg.LinAlgError:
            return {"d2": self.last_d2, "alarm": False, "warm": True}

        # EWMA on the whitened residual gives persistence-aware detection:
        # one odd sample is noise, ten in a row is an event.
        white = delta / np.sqrt(np.maximum(np.diag(cov), 1e-12))
        self.z = (1 - self.lam) * self.z + self.lam * white
        scale = self.lam / (2 - self.lam)
        d2_ewma = float(self.z @ self.z / max(scale, 1e-9))
        d2_inst = float(delta @ resid)
        self.last_d2 = d2_ewma

        warm = self.n < self.warmup
        return {
            "d2": d2_ewma,
            "d2_instant": d2_inst,
            "alarm": (not warm) and d2_ewma > self.threshold,
            "warm": warm,
            "contributions": {s: round(float(v), 2) for s, v in zip(SIGNALS[:self.d], white)},
        }

    def to_dict(self) -> Dict:
        return {"d": self.d, "lam": self.lam, "threshold": self.threshold,
                "shrinkage": self.shrinkage, "warmup": self.warmup,
                "mean": self.mean.tolist(), "cov": self.cov.tolist(),
                "z": self.z.tolist(), "n": self.n}

    @classmethod
    def from_dict(cls, s: Dict) -> "MahalanobisEWMA":
        m = cls(s["d"], s["lam"], s["threshold"], s["shrinkage"], s["warmup"])
        m.mean = np.array(s["mean"], float)
        m.cov = np.array(s["cov"], float)
        m.z = np.array(s["z"], float)
        m.n = s["n"]
        return m


class PageHinkley:
    """Two-sided sequential change detection on a stream of errors."""

    def __init__(self, delta: float = 0.05, lam: float = 8.0, alpha: float = 0.999):
        self.delta = float(delta)
        self.lam = float(lam)
        self.alpha = float(alpha)
        self.mean = 0.0
        self.n = 0
        self.m_pos = 0.0
        self.m_neg = 0.0
        self.n_alarms = 0
        self.last_alarm_ts: Optional[float] = None

    def update(self, value: float, ts: Optional[float] = None) -> bool:
        v = float(value)
        if not np.isfinite(v):
            return False
        self.n += 1
        self.mean += (v - self.mean) / self.n

        self.m_pos = self.alpha * max(0.0, self.m_pos + v - self.mean - self.delta)
        self.m_neg = self.alpha * max(0.0, self.m_neg - v + self.mean - self.delta)

        if self.n > 30 and max(self.m_pos, self.m_neg) > self.lam:
            self.reset_statistics()
            self.n_alarms += 1
            self.last_alarm_ts = ts
            return True
        return False

    def reset_statistics(self) -> None:
        self.m_pos = 0.0
        self.m_neg = 0.0
        self.n = 1

    @property
    def stress(self) -> float:
        """0 to 1: how close we are to declaring drift. Nice on a gauge."""
        return float(min(max(self.m_pos, self.m_neg) / max(self.lam, 1e-9), 1.0))

    def to_dict(self) -> Dict:
        return {"delta": self.delta, "lam": self.lam, "alpha": self.alpha,
                "mean": self.mean, "n": self.n, "m_pos": self.m_pos,
                "m_neg": self.m_neg, "n_alarms": self.n_alarms,
                "last_alarm_ts": self.last_alarm_ts}

    @classmethod
    def from_dict(cls, s: Dict) -> "PageHinkley":
        p = cls(s["delta"], s["lam"], s["alpha"])
        p.__dict__.update({k: s[k] for k in
                           ("mean", "n", "m_pos", "m_neg", "n_alarms", "last_alarm_ts")})
        return p


class SensorHealth:
    RANGES = {
        "temp_c": (-40.0, 85.0),
        "hum": (0.0, 100.0),
        "press_slp": (870.0, 1085.0),
        "cpu_temp": (-20.0, 95.0),
    }

    def __init__(self, window: int = 90):
        self.buffers: Dict[str, Deque[float]] = {
            k: deque(maxlen=window) for k in self.RANGES
        }
        self.flags: Dict[str, str] = {}

    def update(self, obs: Dict[str, float]) -> Dict[str, Dict]:
        report = {}
        for name, (lo, hi) in self.RANGES.items():
            v = obs.get(name)
            if v is None or not np.isfinite(v):
                report[name] = {"status": "missing", "detail": "no reading"}
                continue
            buf = self.buffers[name]
            buf.append(float(v))
            arr = np.asarray(buf, dtype=float)

            if not (lo <= v <= hi):
                status, detail = "fault", f"out of range ({v:.2f})"
            elif arr.size >= 20 and float(np.max(np.abs(np.diff(arr)))) < 1e-9:
                status, detail = "fault", "value latched, sensor may be stuck"
            elif arr.size >= 20 and float(np.std(arr)) < 1e-6:
                status, detail = "warn", "near-zero variance"
            else:
                status, detail = "ok", "nominal"
            report[name] = {"status": status, "detail": detail,
                            "value": float(v), "std": float(np.std(arr)) if arr.size > 2 else 0.0}
        self.flags = {k: v["status"] for k, v in report.items()}
        return report

    @property
    def overall(self) -> str:
        if any(v == "fault" for v in self.flags.values()):
            return "fault"
        if any(v == "warn" for v in self.flags.values()):
            return "warn"
        return "ok"


class AnomalyMonitor:
    """Facade over the three detectors, with a rolling event log."""

    def __init__(self, cfg_model):
        self.novelty = MahalanobisEWMA(
            len(SIGNALS), cfg_model.anomaly_ewma_lambda, cfg_model.anomaly_threshold
        )
        self.drift = PageHinkley(cfg_model.drift_delta, cfg_model.drift_lambda)
        self.health = SensorHealth()
        self.events: Deque[Dict] = deque(maxlen=100)
        self.retrain_requested = False

    def observe(self, ts: float, obs: Dict[str, float]) -> Dict:
        vec = np.array([obs.get(s, np.nan) for s in SIGNALS], dtype=float)
        nov = self.novelty.update(vec)
        health = self.health.update(obs)

        if nov.get("alarm"):
            top = max(nov.get("contributions", {}).items(),
                      key=lambda kv: abs(kv[1]), default=("unknown", 0.0))
            self._log(ts, "novelty", "warn",
                      f"multivariate departure d2={nov['d2']:.1f}, led by {top[0]}")
        for name, rep in health.items():
            if rep["status"] == "fault":
                self._log(ts, "sensor", "error", f"{name}: {rep['detail']}")

        return {
            "novelty": nov,
            "health": health,
            "health_overall": self.health.overall,
            "drift": {
                "stress": self.drift.stress,
                "alarms": self.drift.n_alarms,
                "last_alarm_ts": self.drift.last_alarm_ts,
                "retrain_requested": self.retrain_requested,
            },
        }

    def observe_error(self, ts: float, abs_error: float) -> bool:
        """Feed a matured forecast error; returns True if drift was declared."""
        fired = self.drift.update(abs_error, ts)
        if fired:
            self.retrain_requested = True
            self._log(ts, "drift", "warn",
                      "forecast error distribution shifted, retrain queued")
        return fired

    def clear_retrain_flag(self) -> None:
        self.retrain_requested = False

    def _log(self, ts: float, kind: str, severity: str, detail: str) -> None:
        self.events.append({"ts": ts, "kind": kind, "severity": severity, "detail": detail})

    def recent(self, n: int = 20) -> List[Dict]:
        return list(self.events)[-n:][::-1]

    def to_dict(self) -> Dict:
        return {"novelty": self.novelty.to_dict(), "drift": self.drift.to_dict(),
                "events": list(self.events), "retrain_requested": self.retrain_requested}

    def load_dict(self, s: Dict) -> None:
        self.novelty = MahalanobisEWMA.from_dict(s["novelty"])
        self.drift = PageHinkley.from_dict(s["drift"])
        self.events = deque(s.get("events", []), maxlen=100)
        self.retrain_requested = s.get("retrain_requested", False)
