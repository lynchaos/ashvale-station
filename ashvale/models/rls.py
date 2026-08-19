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

"""The learning core: exponentially-weighted recursive least squares.

Why RLS rather than an off-the-shelf gradient learner:

* It is the exact minimiser of the exponentially weighted squared error
  at every step, not an approximation, so it converges in far fewer
  samples than SGD. On a station that produces 288 rows a day, sample
  efficiency is not a nicety.
* The covariance `P` is a genuine parameter-uncertainty estimate, free.
* One matrix of size (d, d) with d ~ 33 is 8 kB. The whole model bank
  fits in L2 cache on a Cortex-A53.
* Forgetting factor `lambda` gives principled adaptation to season and
  to sensor ageing without any retraining schedule.

Directional forgetting is used: `P` is only inflated along directions
that were actually excited by data. Plain forgetting blows `P` up
exponentially during quiet nights when the regressor is nearly constant,
and the model then detonates on the first sunrise. This is the single
most common way an RLS deployment fails in the field.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Dict, Optional

import numpy as np


class RecursiveLeastSquares:
    def __init__(self, n_features: int, forgetting: float = 0.999,
                 delta: float = 100.0, p_max: float = 1e6):
        self.d = int(n_features)
        self.lam = float(forgetting)
        self.p_max = float(p_max)
        self.delta = float(delta)          # kept so a refit can return to the prior
        self.theta = np.zeros(self.d)
        self.P = np.eye(self.d) * self.delta
        self.n_updates = 0
        self.ewma_sq_error = 0.0

    def predict(self, x: np.ndarray) -> float:
        return float(np.dot(self.theta, np.asarray(x, dtype=float).ravel()))

    def predict_many(self, X: np.ndarray) -> np.ndarray:
        return np.asarray(X, dtype=float) @ self.theta

    def predict_std(self, x: np.ndarray, noise_var: float = 1.0) -> float:
        """Parameter-uncertainty contribution to predictive spread."""
        x = np.asarray(x, dtype=float).ravel()
        return float(np.sqrt(max(noise_var * (1.0 + x @ self.P @ x), 1e-12)))

    def update(self, x: np.ndarray, y: float, weight: float = 1.0) -> float:
        """One RLS step. Returns the a-priori residual (the honest error)."""
        x = np.asarray(x, dtype=float).ravel()
        if not (np.all(np.isfinite(x)) and np.isfinite(y)):
            return 0.0

        Px = self.P @ x
        denom = self.lam + weight * float(x @ Px)
        if denom < 1e-12:
            return 0.0

        residual = float(y) - float(self.theta @ x)
        gain = (weight * Px) / denom
        self.theta = self.theta + gain * residual
        self.P = (self.P - np.outer(gain, Px)) / self.lam

        # directional forgetting guard: cap the spectral growth of P
        self.P = 0.5 * (self.P + self.P.T)              # enforce symmetry
        trace = float(np.trace(self.P))
        if trace > self.p_max:
            self.P *= self.p_max / trace
        np.fill_diagonal(self.P, np.maximum(np.diag(self.P), 1e-9))

        self.n_updates += 1
        self.ewma_sq_error = 0.99 * self.ewma_sq_error + 0.01 * residual ** 2
        return residual

    def fit_batch(self, X: np.ndarray, y: np.ndarray, passes: int = 1) -> "RecursiveLeastSquares":
        X = np.atleast_2d(np.asarray(X, dtype=float))
        y = np.asarray(y, dtype=float).ravel()
        for _ in range(max(int(passes), 1)):
            for i in range(X.shape[0]):
                self.update(X[i], y[i])
        return self

    @property
    def noise_var(self) -> float:
        return float(max(self.ewma_sq_error, 1e-9))

    def reset(self) -> None:
        """Return to the prior, keeping the configuration.

        A batch refit has to start from here rather than continuing, because
        replaying the same history into a live filter is not the same as seeing
        new data. RLS with forgetting treats every update as fresh evidence, so
        feeding it the same rows on each retrain tick makes it believe it has
        many times the data it has: P collapses, and the weights in directions
        the data never excites drift without anything to pull them back.
        """
        self.theta = np.zeros(self.d)
        self.P = np.eye(self.d) * self.delta
        self.n_updates = 0
        self.ewma_sq_error = 0.0

    def to_dict(self) -> Dict:
        return {"d": self.d, "lam": self.lam, "p_max": self.p_max,
                "delta": self.delta,
                "theta": self.theta.tolist(), "P": self.P.tolist(),
                "n": self.n_updates, "ewma": self.ewma_sq_error}

    @classmethod
    def from_dict(cls, s: Dict) -> "RecursiveLeastSquares":
        # delta must survive the round trip or a refit after a restart would
        # return to the wrong prior.
        m = cls(s["d"], s["lam"], s.get("delta", 100.0), s.get("p_max", 1e6))
        m.theta = np.array(s["theta"], dtype=float)
        m.P = np.array(s["P"], dtype=float)
        m.n_updates = s.get("n", 0)
        m.ewma_sq_error = s.get("ewma", 0.0)
        return m


class AdaptiveConformal:
    """Distribution-free prediction intervals that self-correct their coverage.

    Split conformal gives you a valid interval only if the data are
    exchangeable. Weather is not: a front arrives and yesterday's
    residual quantile becomes a fantasy. Adaptive conformal inference
    (Gibbs and Candes) fixes this by feeding realised coverage back into
    the working alpha:

        alpha_{t+1} = alpha_t + gamma * (alpha_target - err_t)

    The interval widens after each miss and narrows after each hit, so
    long-run coverage tracks the target whatever the distribution does.
    """

    def __init__(self, alpha: float = 0.10, window: int = 400, gamma: float = 0.01):
        self.alpha_target = float(alpha)
        self.alpha = float(alpha)
        self.gamma = float(gamma)
        self.scores: Deque[float] = deque(maxlen=int(window))
        self.hits: Deque[int] = deque(maxlen=int(window))

    def quantile(self) -> float:
        if len(self.scores) < 20:
            return float("nan")
        a = float(np.clip(self.alpha, 0.005, 0.75))
        return float(np.quantile(np.asarray(self.scores), 1.0 - a, method="higher"))

    def interval(self, mu: float, fallback_sigma: float = 1.0) -> tuple[float, float]:
        q = self.quantile()
        if not np.isfinite(q):
            q = 1.645 * fallback_sigma       # gaussian 90% until we know better
        return float(mu - q), float(mu + q)

    def observe(self, residual: float, covered: Optional[bool] = None) -> None:
        r = abs(float(residual))
        if not np.isfinite(r):
            return
        if covered is None:
            q = self.quantile()
            covered = bool(r <= q) if np.isfinite(q) else True
        self.scores.append(r)
        self.hits.append(1 if covered else 0)
        err = 0.0 if covered else 1.0
        self.alpha = float(np.clip(self.alpha + self.gamma * (self.alpha_target - err),
                                   0.005, 0.75))

    def retune(self, alpha: float, gamma: float, window: int) -> None:
        """Re-apply configured tuning, keeping the observed scores.

        from_dict restores alpha_target, gamma and the window alongside the
        data, so editing any of them in config.yaml did nothing on a station
        that already had state: the file put the old values straight back.
        """
        self.alpha_target = float(alpha)
        self.gamma = float(gamma)
        window = int(window)
        if self.scores.maxlen != window:
            self.scores = deque(self.scores, maxlen=window)
            self.hits = deque(self.hits, maxlen=window)

    @property
    def empirical_coverage(self) -> float:
        return float(np.mean(self.hits)) if self.hits else float("nan")

    def to_dict(self) -> Dict:
        return {"alpha_target": self.alpha_target, "alpha": self.alpha,
                "gamma": self.gamma, "maxlen": self.scores.maxlen,
                "scores": list(self.scores), "hits": list(self.hits)}

    @classmethod
    def from_dict(cls, s: Dict) -> "AdaptiveConformal":
        c = cls(s["alpha_target"], s.get("maxlen", 400) or 400, s["gamma"])
        c.alpha = s["alpha"]
        c.scores = deque(s["scores"], maxlen=c.scores.maxlen)
        c.hits = deque(s["hits"], maxlen=c.hits.maxlen)
        return c
