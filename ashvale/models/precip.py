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

"""Will it rain? A prior with a hundred years of service, plus a learner.

Two components, deliberately:

1. `zambretti()` is the 1915 Negretti and Zambra slide-rule algorithm,
   re-expressed here in the standard three-branch form. It needs only
   sea-level pressure, its tendency and the season. It has no parameters
   to overfit, it works from the first hour of deployment, and in the
   temperate maritime climate it was designed for it is genuinely hard
   to beat with a small dataset. It is the prior.

2. `PrecipitationModel` is an online logistic regression that learns the
   residual: what your specific location does that the slide rule does
   not know. It starts from the Zambretti logit and only earns influence
   as labels accumulate, so it cannot embarrass you on day one.

Labels are the hard part, and the design is explicit about it. Without a
rain gauge, a *proxy* label is used (near-saturated air with a collapsing
dew-point depression), and it is flagged as weak. `POST /api/label` lets
you supply ground truth from a window: two seconds of your attention is
worth a week of proxy labels, and the learner weights them accordingly.
"""

from __future__ import annotations

import math
import time
from typing import Dict, List, Optional, Tuple

import numpy as np

# Severity classes the Z number maps onto. Wording is ours, not the
# original card's, and is deliberately about actionable state rather
# than Edwardian poetry.
_CONDITIONS = [
    (1, 2, "settled", "Settled and dry"),
    (3, 5, "fine", "Fine, little change expected"),
    (6, 8, "fair", "Fair, becoming less settled"),
    (9, 12, "changeable", "Changeable, showers possible"),
    (13, 16, "unsettled", "Unsettled, rain at times"),
    (17, 20, "rain", "Rain likely, turning wet"),
    (21, 23, "wet", "Wet and windy"),
    (24, 26, "stormy", "Stormy, heavy rain likely"),
]

_RAIN_PRIOR = {
    "settled": 0.03, "fine": 0.07, "fair": 0.15, "changeable": 0.32,
    "unsettled": 0.52, "rain": 0.72, "wet": 0.85, "stormy": 0.93,
}

FEATURES = ["bias", "slp_anom", "tend_1h", "tend_3h", "tend_6h", "rh",
            "dew_depression", "dew_dep_rate", "cloud_index", "temp_dev",
            "wet_bulb_depression", "zambretti_logit"]


def _season_is_summer(ts: Optional[float], latitude: float) -> bool:
    month = time.gmtime(ts or time.time()).tm_mon
    northern = latitude >= 0
    summer_months = {4, 5, 6, 7, 8, 9}
    return (month in summer_months) if northern else (month not in summer_months)


BARO_BOTTOM = 950.0
BARO_TOP = 1050.0

# Each branch maps normalised pressure onto a slice of the 26-point scale.
# The ordering is the whole point of the instrument: for a given pressure,
# rising air is always a better forecast than falling air, and within a
# branch higher pressure is always better. Ranges overlap because a deep
# but rising low really is more hopeful than a shallow but falling high.
_BRANCH = {
    "rising": (1.0, 10.0),
    "steady": (6.0, 17.0),
    "falling": (11.0, 26.0),
}


def zambretti(slp_hpa: float, tendency_hpa_per_h: float,
              ts: Optional[float] = None, latitude: float = 52.0,
              steady_band: float = 0.10) -> Dict:
    """Three-branch barometric forecast on the Zambretti 26-point scale.

    The 1915 Negretti and Zambra slide rule read pressure, its tendency and
    the season off a rotating card and returned one of 26 outcomes, 1 being
    settled and 26 being stormy. Published transcriptions of its constants
    disagree with each other, so rather than mis-cite one, this is an
    explicit re-parameterisation onto the same 26-point scale, anchored to
    the behaviour the instrument is actually known for:

        rising pressure  -> lower Z (improving)
        falling pressure -> higher Z (deteriorating)
        higher pressure  -> lower Z within any branch

    Getting that sign wrong is easy and produces confident nonsense: a
    barometer climbing hard while the panel reads `stormy` is the tell.

    Args:
        slp_hpa: pressure reduced to mean sea level. Passing station
            pressure here is a common and silent bug: at 100 m elevation
            it shifts the result by about two categories, permanently.
        tendency_hpa_per_h: Kalman-filtered rate, not a finite difference.
        steady_band: |tendency| below this counts as steady.
    """
    p = float(np.clip(slp_hpa, BARO_BOTTOM, BARO_TOP))
    tend = float(tendency_hpa_per_h)
    summer = _season_is_summer(ts, latitude)

    if tend <= -steady_band:
        trend = "falling"
    elif tend >= steady_band:
        trend = "rising"
    else:
        trend = "steady"

    lo, hi = _BRANCH[trend]
    u = (p - BARO_BOTTOM) / (BARO_TOP - BARO_BOTTOM)     # 0 at 950, 1 at 1050
    z = lo + (hi - lo) * (1.0 - u)

    # Seasonal nudge: summer lows are typically convective and shorter lived,
    # winter lows are frontal and grimmer. One category either way.
    if trend == "falling":
        z += -1.0 if summer else 1.0
    elif trend == "rising":
        z += -1.0 if summer else 1.0

    z_int = int(np.clip(round(z), 1, 26))
    condition, label = "changeable", "Changeable"
    for lo, hi, key, text in _CONDITIONS:
        if lo <= z_int <= hi:
            condition, label = key, text
            break

    return {
        "z": z_int,
        "trend": trend,
        "condition": condition,
        "label": label,
        "prior_rain_prob": _RAIN_PRIOR[condition],
        "slp_used": p,
        "tendency": tend,
        "season": "summer" if summer else "winter",
    }


def tendency_code(tend_hpa_per_h: float) -> str:
    """WMO-style pressure characteristic, the thing sailors actually read."""
    t = float(tend_hpa_per_h)
    if t <= -1.5:
        return "falling very rapidly"
    if t <= -0.6:
        return "falling rapidly"
    if t <= -0.15:
        return "falling"
    if t < 0.15:
        return "steady"
    if t < 0.6:
        return "rising"
    if t < 1.5:
        return "rising rapidly"
    return "rising very rapidly"


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-float(np.clip(z, -30.0, 30.0))))


def _logit(p: float) -> float:
    p = float(np.clip(p, 1e-4, 1 - 1e-4))
    return math.log(p / (1 - p))


class PrecipitationModel:
    """Online logistic regression on top of the Zambretti logit.

    Trained by AdaGrad because feature scales here vary by two orders of
    magnitude and a fixed learning rate would either crawl on `tendency`
    or explode on `rh`. The `zambretti_logit` feature is initialised with
    a coefficient of 1.0 so the model *starts* as the slide rule and
    departs from it only where the data insist.
    """

    def __init__(self, lr: float = 0.08, l2: float = 1e-4):
        self.w = np.zeros(len(FEATURES))
        self.w[FEATURES.index("zambretti_logit")] = 1.0
        self.g2 = np.ones(len(FEATURES)) * 1e-3
        self.lr = float(lr)
        self.l2 = float(l2)
        self.n_strong = 0
        self.n_weak = 0
        self.ewma_logloss = 0.693      # log 2, the coin-flip baseline
        self.mean = np.zeros(len(FEATURES))
        self.m2 = np.ones(len(FEATURES))
        self.n_seen = 0

    # -------------------------------------------------------- features

    def featurise(self, obs: Dict, zam: Dict) -> np.ndarray:
        x = np.array([
            1.0,
            obs.get("slp", 1013.25) - 1013.25,
            obs.get("tend_1h", 0.0),
            obs.get("tend_3h", 0.0),
            obs.get("tend_6h", 0.0),
            (obs.get("rh", 60.0) - 70.0) / 10.0,
            obs.get("dew_depression", 5.0),
            obs.get("dew_dep_rate", 0.0),
            obs.get("cloud_index", 0.5),
            obs.get("temp_dev", 0.0),
            obs.get("wet_bulb_depression", 2.0),
            _logit(zam["prior_rain_prob"]),
        ], dtype=float)
        return np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)

    def _standardise(self, x: np.ndarray, update: bool) -> np.ndarray:
        if update:
            self.n_seen += 1
            delta = x - self.mean
            self.mean += delta / self.n_seen
            self.m2 += delta * (x - self.mean)
        if self.n_seen < 20:
            z = x.copy()
        else:
            std = np.sqrt(self.m2 / max(self.n_seen - 1, 1))
            std = np.where(std < 1e-8, 1.0, std)
            z = (x - self.mean) / std
        z[0] = 1.0
        # keep the prior feature unscaled: its units are already logits
        z[FEATURES.index("zambretti_logit")] = x[FEATURES.index("zambretti_logit")]
        return z

    # ------------------------------------------------------- inference

    def predict(self, obs: Dict, zam: Dict) -> Dict:
        x = self._standardise(self.featurise(obs, zam), update=False)
        p_model = _sigmoid(float(self.w @ x))
        p_prior = zam["prior_rain_prob"]
        # trust the learner in proportion to how many strong labels it has
        trust = self.n_strong / (self.n_strong + 25.0)
        p = trust * p_model + (1 - trust) * p_prior
        return {
            "rain_probability": float(np.clip(p, 0.0, 1.0)),
            "model_probability": float(p_model),
            "prior_probability": float(p_prior),
            "learner_trust": float(trust),
            "condition": zam["condition"],
            "label": zam["label"],
            "zambretti_z": zam["z"],
            "pressure_characteristic": tendency_code(zam["tendency"]),
            "tendency": float(zam["tendency"]),
            "sea_level_pressure": float(zam["slp_used"]),
            "strong_labels": self.n_strong,
            "weak_labels": self.n_weak,
            "logloss_ewma": round(float(self.ewma_logloss), 4),
        }

    # -------------------------------------------------------- learning

    def learn(self, obs: Dict, zam: Dict, y: float, strong: bool = False) -> float:
        """AdaGrad step. Weak (proxy) labels get a tenth of the weight."""
        x = self._standardise(self.featurise(obs, zam), update=True)
        p = _sigmoid(float(self.w @ x))
        weight = 1.0 if strong else 0.1
        grad = weight * (p - float(y)) * x + self.l2 * self.w
        self.g2 += grad ** 2
        self.w -= self.lr * grad / np.sqrt(self.g2)

        loss = -(y * math.log(max(p, 1e-9)) + (1 - y) * math.log(max(1 - p, 1e-9)))
        self.ewma_logloss = 0.98 * self.ewma_logloss + 0.02 * loss
        if strong:
            self.n_strong += 1
        else:
            self.n_weak += 1
        return float(loss)

    def coefficients(self) -> List[Dict]:
        return [{"feature": f, "weight": round(float(w), 4)}
                for f, w in zip(FEATURES, self.w)]

    def to_dict(self) -> Dict:
        return {"w": self.w.tolist(), "g2": self.g2.tolist(), "lr": self.lr,
                "l2": self.l2, "n_strong": self.n_strong, "n_weak": self.n_weak,
                "ewma_logloss": self.ewma_logloss, "mean": self.mean.tolist(),
                "m2": self.m2.tolist(), "n_seen": self.n_seen}

    def load_dict(self, s: Dict) -> None:
        self.w = np.array(s["w"], dtype=float)
        self.g2 = np.array(s["g2"], dtype=float)
        self.lr, self.l2 = s["lr"], s["l2"]
        self.n_strong, self.n_weak = s["n_strong"], s["n_weak"]
        self.ewma_logloss = s["ewma_logloss"]
        self.mean = np.array(s["mean"], dtype=float)
        self.m2 = np.array(s["m2"], dtype=float)
        self.n_seen = s["n_seen"]


def proxy_wet_label(rh: float, dew_depression: float, cloud_index: float) -> Optional[float]:
    """A weak, deliberately conservative stand-in for a rain gauge.

    Returns 1.0 for near-saturated overcast air, 0.0 for clearly dry air,
    and None in the ambiguous middle, where a guess would poison the
    training set faster than the extra samples could help.
    """
    if not all(np.isfinite([rh, dew_depression, cloud_index])):
        return None
    if rh >= 93.0 and dew_depression <= 1.2 and cloud_index >= 0.6:
        return 1.0
    if rh <= 65.0 and dew_depression >= 5.0:
        return 0.0
    return None
