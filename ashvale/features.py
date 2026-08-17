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

"""Feature engineering, pure numpy, no pandas.

Design rules used here:

* Anything derivable from physics is computed, not learned.
* Anything periodic is encoded as sin/cos pairs so a linear model can
  represent phase without a discontinuity at midnight.
* Every lag is expressed in *hours*, not samples, so changing `grid_s`
  does not silently change what the model means by `three hours ago`.
* Targets are predicted as *deltas from now*, never as absolute levels.
  A model that must output 14.7 C spends all its capacity on the mean;
  a model that outputs +0.4 C spends it on the weather.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np

from .physics import (
    absolute_humidity,
    clear_sky_irradiance,
    dew_point,
    solar_position,
    vapour_pressure_deficit,
    wet_bulb,
)

FEATURE_NAMES: List[str] = [
    "bias",
    "temp", "temp_rate_1h", "temp_rate_3h", "temp_std_3h", "temp_dev_24h",
    "hum", "hum_rate_1h", "hum_rate_3h", "hum_std_3h",
    "press_anom", "press_tend_1h", "press_tend_3h", "press_tend_6h", "press_std_6h",
    "dewpoint", "dewpoint_depression", "vpd", "abs_hum", "wet_bulb",
    "log_lux", "cloud_index", "solar_elev", "solar_elev_pos", "is_day",
    "sin_h1", "cos_h1", "sin_h2", "cos_h2", "sin_doy", "cos_doy",
    "press_x_hum", "tend_x_dewdep",
]

N_FEATURES = len(FEATURE_NAMES)


def _shift(a: np.ndarray, k: int) -> np.ndarray:
    """a[i - k], NaN-padded at the front."""
    out = np.full_like(a, np.nan, dtype=float)
    if k <= 0:
        return a.copy()
    if k < a.size:
        out[k:] = a[:-k]
    return out


def _rolling(a: np.ndarray, win: int, fn) -> np.ndarray:
    """Trailing rolling statistic. O(n*win) but win is small and n is a day."""
    out = np.full(a.size, np.nan, dtype=float)
    if a.size == 0:
        return out
    win = max(int(win), 1)
    for i in range(a.size):
        lo = max(0, i - win + 1)
        seg = a[lo:i + 1]
        seg = seg[np.isfinite(seg)]
        if seg.size >= max(2, win // 3):
            out[i] = fn(seg)
    return out


def build_features(grid_ts: np.ndarray, temp: np.ndarray, hum: np.ndarray,
                   press_slp: np.ndarray, lux: np.ndarray,
                   grid_s: int, latitude: float, longitude: float,
                   min_days_annual: float = 120.0
                   ) -> Tuple[np.ndarray, np.ndarray]:
    """Return (X of shape (n, N_FEATURES), valid mask of shape (n,))."""
    n = grid_ts.size
    if n == 0:
        return np.zeros((0, N_FEATURES)), np.zeros(0, dtype=bool)

    per_hour = max(int(round(3600 / grid_s)), 1)

    def rate(a: np.ndarray, hours: int) -> np.ndarray:
        return (a - _shift(a, hours * per_hour)) / float(hours)

    temp_rate_1h = rate(temp, 1)
    temp_rate_3h = rate(temp, 3)
    temp_std_3h = _rolling(temp, 3 * per_hour, np.std)
    temp_mean_24h = _rolling(temp, 24 * per_hour, np.mean)
    temp_dev_24h = temp - temp_mean_24h

    hum_rate_1h = rate(hum, 1)
    hum_rate_3h = rate(hum, 3)
    hum_std_3h = _rolling(hum, 3 * per_hour, np.std)

    press_anom = press_slp - 1013.25
    press_tend_1h = rate(press_slp, 1)
    press_tend_3h = rate(press_slp, 3)
    press_tend_6h = rate(press_slp, 6)
    press_std_6h = _rolling(press_slp, 6 * per_hour, np.std)

    dp = dew_point(temp, hum)
    dep = temp - dp
    vpd = vapour_pressure_deficit(temp, hum)
    ah = absolute_humidity(temp, hum)
    wb = wet_bulb(temp, hum)

    elev, _ = solar_position(grid_ts, latitude, longitude)
    elev = np.atleast_1d(elev)
    expected = clear_sky_irradiance(elev)
    log_lux = np.log1p(np.clip(lux, 0.0, None))
    # cloud index: 1 = overcast, 0 = clear. Only meaningful in daylight.
    scale = np.maximum(expected, 1.0) * 45.0     # crude lux-per-W/m^2 for daylight
    cloud = np.where(elev > 5.0, np.clip(1.0 - np.clip(lux, 0, None) / scale, 0.0, 1.0), 0.5)

    hour = (grid_ts % 86400.0) / 86400.0
    doy = (grid_ts % 31557600.0) / 31557600.0

    # Annual harmonics are held at zero until the record spans enough of a year
    # to excite them, exactly as the climatology fit already gates its annual
    # terms. Left on from day one they are near-constant, near-collinear with
    # each other and with the bias, and RLS answers that rank-deficient system
    # with enormous cancelling weights. Measured on a real station after 1.5
    # days: cos_doy +1191, sin_doy +1174, ||theta|| 1680 against a median |theta|
    # of 1.67, cond(P) 3.1e9, and a six hour forecast of 53 C in a 24 C room.
    # Zero is the honest value: with a day and a half of data the station knows
    # nothing whatsoever about the season.
    span_days = float(grid_ts[-1] - grid_ts[0]) / 86400.0 if n > 1 else 0.0
    annual_on = 1.0 if span_days >= min_days_annual else 0.0
    sin_doy = np.sin(2 * np.pi * doy) * annual_on
    cos_doy = np.cos(2 * np.pi * doy) * annual_on

    X = np.column_stack([
        np.ones(n),
        temp, temp_rate_1h, temp_rate_3h, temp_std_3h, temp_dev_24h,
        hum, hum_rate_1h, hum_rate_3h, hum_std_3h,
        press_anom, press_tend_1h, press_tend_3h, press_tend_6h, press_std_6h,
        dp, dep, vpd, ah, wb,
        log_lux, cloud, elev, np.clip(elev, 0.0, None), (elev > 0.0).astype(float),
        np.sin(2 * np.pi * hour), np.cos(2 * np.pi * hour),
        np.sin(4 * np.pi * hour), np.cos(4 * np.pi * hour),
        sin_doy, cos_doy,
        press_anom * (hum - 70.0) / 100.0,
        press_tend_3h * dep,
    ])

    assert X.shape[1] == N_FEATURES, f"feature count drift: {X.shape[1]} vs {N_FEATURES}"
    valid = np.all(np.isfinite(X), axis=1)
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    return X, valid


class Standardiser:
    """Streaming z-scoring with Welford moments.

    Recursive least squares is scale-sensitive: an unscaled `pressure` at
    1013 and an unscaled `temp_rate` at 0.02 give a condition number that
    will embarrass you. Standardising online keeps P well-conditioned
    without a second pass over history.
    """

    def __init__(self, n_features: int = N_FEATURES):
        self.n = 0
        self.mean = np.zeros(n_features)
        self.m2 = np.ones(n_features)

    def partial_fit(self, X: np.ndarray) -> None:
        for row in np.atleast_2d(X):
            self.n += 1
            delta = row - self.mean
            self.mean += delta / self.n
            self.m2 += delta * (row - self.mean)

    def transform(self, X: np.ndarray) -> np.ndarray:
        if self.n < 2:
            return np.atleast_2d(X)
        std = np.sqrt(self.m2 / max(self.n - 1, 1))
        # 1e-8 was a token guard: it only catches a bit-exactly constant column.
        # A feature that merely barely moves sails through and gets divided by
        # its own noise, which manufactures a large z-score out of nothing. A
        # feature with this little spread carries no information, so scale it by
        # one and let it stay near zero rather than amplifying it.
        std = np.where(std < 1e-3, 1.0, std)
        out = (np.atleast_2d(X) - self.mean) / std
        out[:, 0] = 1.0            # keep the bias column intact
        return out

    def fit_transform(self, X: np.ndarray) -> np.ndarray:
        self.partial_fit(X)
        return self.transform(X)

    def to_dict(self) -> Dict:
        return {"n": self.n, "mean": self.mean.tolist(), "m2": self.m2.tolist()}

    @classmethod
    def from_dict(cls, d: Dict) -> "Standardiser":
        s = cls(len(d["mean"]))
        s.n = d["n"]
        s.mean = np.array(d["mean"], dtype=float)
        s.m2 = np.array(d["m2"], dtype=float)
        return s


def supervised_pairs(X: np.ndarray, valid: np.ndarray, y: np.ndarray,
                     horizon_steps: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Align features at t with the *change* in y between t and t+h.

    Returns (X_aligned, delta_y, anchor_y) so the caller can reconstruct
    the absolute forecast as anchor + predicted delta.
    """
    n = X.shape[0]
    if n <= horizon_steps:
        return np.zeros((0, X.shape[1])), np.zeros(0), np.zeros(0)
    Xa = X[:n - horizon_steps]
    anchor = y[:n - horizon_steps]
    future = y[horizon_steps:]
    mask = (valid[:n - horizon_steps] & np.isfinite(future) & np.isfinite(anchor))
    return Xa[mask], (future - anchor)[mask], anchor[mask]
