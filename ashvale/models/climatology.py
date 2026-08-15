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

"""Harmonic regression: the long-range half of the forecast.

An honest statement first, because a weather product that oversells
itself is worse than no product. A single point sensor cannot see a
front approaching from the Atlantic. Beyond roughly twelve hours, the
only information your station holds is:

  * where in the diurnal cycle you are,
  * where in the annual cycle you are,
  * the current synoptic pressure anomaly and its tendency,
  * the local trend of the last few days.

So that is exactly what this model uses. It is a ridge-regularised
Fourier basis in time-of-day and day-of-year, plus a slow linear trend
and a pressure-anomaly coupling. Days 2 to 7 are a *climatological
outlook with an anomaly correction*, not a forecast, and the API labels
them as such. Anything more confident would be theatre.

The annual harmonics only switch on once the station has enough history
to identify them (`climatology_min_days_annual`, default 120). Before
that, fitting a 365-day sine to three weeks of data produces a
magnificent extrapolation straight off the edge of the physical world.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

DAY = 86400.0
YEAR = 365.2422 * DAY


class HarmonicClimatology:
    def __init__(self, targets, diurnal_harmonics: int = 3,
                 annual_harmonics: int = 2, ridge: float = 1.0,
                 min_days_annual: float = 120.0):
        self.targets = tuple(targets)
        self.kd = int(diurnal_harmonics)
        self.ka = int(annual_harmonics)
        self.ridge = float(ridge)
        self.min_days_annual = float(min_days_annual)
        self.coef: Dict[str, np.ndarray] = {}
        self.resid_std: Dict[str, float] = {}
        self.t0: float = 0.0
        self.use_annual = False
        self.n_days = 0.0
        self.ready = False

    # ---------------------------------------------------------- basis

    def _design(self, ts: np.ndarray) -> np.ndarray:
        ts = np.atleast_1d(np.asarray(ts, dtype=float))
        t_days = (ts - self.t0) / DAY
        cols = [np.ones(ts.size), t_days / 30.0]          # slow trend, per month
        for k in range(1, self.kd + 1):
            w = 2 * np.pi * k * ts / DAY
            cols += [np.sin(w), np.cos(w)]
        if self.use_annual:
            for k in range(1, self.ka + 1):
                w = 2 * np.pi * k * ts / YEAR
                cols += [np.sin(w), np.cos(w)]
        return np.column_stack(cols)

    # ------------------------------------------------------------ fit

    def fit(self, ts: np.ndarray, series: Dict[str, np.ndarray],
            valid: Optional[np.ndarray] = None) -> Dict[str, float]:
        ts = np.asarray(ts, dtype=float)
        if ts.size < 48:
            self.ready = False
            return {}
        self.t0 = float(ts[0])
        self.n_days = float((ts[-1] - ts[0]) / DAY)
        self.use_annual = self.n_days >= self.min_days_annual

        A = self._design(ts)
        mask = np.ones(ts.size, dtype=bool) if valid is None else valid.astype(bool)
        out = {}
        for target in self.targets:
            y = np.asarray(series.get(target, np.empty(0)), dtype=float)
            if y.size != ts.size:
                continue
            m = mask & np.isfinite(y)
            if m.sum() < A.shape[1] * 3:
                continue
            Am, ym = A[m], y[m]
            # ridge: leave the intercept unpenalised
            reg = np.eye(A.shape[1]) * self.ridge
            reg[0, 0] = 0.0
            beta = np.linalg.solve(Am.T @ Am + reg, Am.T @ ym)
            self.coef[target] = beta
            resid = ym - Am @ beta
            self.resid_std[target] = float(np.std(resid))
            out[target] = self.resid_std[target]
        self.ready = bool(self.coef)
        return out

    # -------------------------------------------------------- predict

    def predict(self, target: str, ts: np.ndarray) -> np.ndarray:
        ts = np.atleast_1d(np.asarray(ts, dtype=float))
        beta = self.coef.get(target)
        if beta is None:
            return np.zeros(ts.size)
        return self._design(ts) @ beta

    def outlook(self, target: str, now: float, days: int = 7,
                step_s: int = 3 * 3600, anomaly: float = 0.0,
                anomaly_halflife_h: float = 30.0) -> List[Dict]:
        """Climatology plus an exponentially decaying current anomaly.

        The anomaly term is what makes this better than a textbook: if
        today is 3 C above the seasonal norm, tomorrow morning probably
        still is, and next Thursday almost certainly is not. The decay
        half-life encodes exactly that intuition, and the interval widens
        with the square root of lead time as any diffusive process should.
        """
        if not self.ready or target not in self.coef:
            return []
        grid = np.arange(now, now + days * DAY, step_s, dtype=float)
        base = self.predict(target, grid)
        lead_h = (grid - now) / 3600.0
        decay = 0.5 ** (lead_h / max(anomaly_halflife_h, 1e-3))
        mu = base + anomaly * decay
        sigma0 = self.resid_std.get(target, 1.0)
        sigma = sigma0 * np.sqrt(1.0 + lead_h / 24.0)
        return [
            {"ts": float(t), "lead_h": float(lh), "mu": float(m),
             "lo": float(m - 1.645 * s), "hi": float(m + 1.645 * s)}
            for t, lh, m, s in zip(grid, lead_h, mu, sigma)
        ]

    def anomaly_now(self, target: str, ts: float, observed: float) -> float:
        if not self.ready or target not in self.coef:
            return 0.0
        return float(observed - self.predict(target, np.array([ts]))[0])

    def to_dict(self) -> Dict:
        return {"targets": list(self.targets), "kd": self.kd, "ka": self.ka,
                "ridge": self.ridge, "min_days_annual": self.min_days_annual,
                "t0": self.t0, "use_annual": self.use_annual, "n_days": self.n_days,
                "coef": {k: v.tolist() for k, v in self.coef.items()},
                "resid_std": self.resid_std, "ready": self.ready}

    def load_dict(self, s: Dict) -> None:
        self.kd, self.ka = s["kd"], s["ka"]
        self.ridge = s["ridge"]
        self.min_days_annual = s["min_days_annual"]
        self.t0 = s["t0"]
        self.use_annual = s["use_annual"]
        self.n_days = s.get("n_days", 0.0)
        self.coef = {k: np.array(v, dtype=float) for k, v in s["coef"].items()}
        self.resid_std = s["resid_std"]
        self.ready = s["ready"]
