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

"""Multi-horizon forecasting: one direct head per (target, horizon).

Direct rather than recursive. A recursive one-step model iterated 288
times to reach 24 hours compounds its own bias into a beautifully smooth
lie. Direct heads cost more memory (six horizons x three targets = 18
small models, about 150 kB total) and are worth every byte.

Each head predicts a *delta from now*, then the ensemble blends three
opinions with weights that are themselves learned online:

    persistence  : it will be exactly as it is now
    climatology  : it will be whatever this hour of this day usually is
    learned RLS  : it will be now plus what the regressors imply

Persistence wins at 15 minutes. Climatology wins at 24 hours. The RLS
head wins in the middle, which is exactly the region a physical
forecaster finds hardest. The blend weights are updated by exponentiated
gradient (Hedge), so the ensemble is never worse than its best member by
more than a log factor, and it re-weights itself within a day when the
season turns.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np

from ..features import N_FEATURES, Standardiser, supervised_pairs
from .rls import AdaptiveConformal, RecursiveLeastSquares

MEMBERS = ("persistence", "climatology", "learned")


class ForecastHead:
    """One target, one horizon."""

    def __init__(self, target: str, horizon_s: int, n_features: int = N_FEATURES,
                 forgetting: float = 0.9985, delta: float = 100.0,
                 alpha: float = 0.10, conformal_window: int = 400,
                 gamma: float = 0.01, hedge_eta: float = 0.35):
        self.target = target
        self.horizon_s = int(horizon_s)
        self.model = RecursiveLeastSquares(n_features, forgetting, delta)
        self.conformal = AdaptiveConformal(alpha, conformal_window, gamma)
        self.weights = np.ones(len(MEMBERS)) / len(MEMBERS)
        self.eta = float(hedge_eta)
        self.member_mae = np.zeros(len(MEMBERS))
        self.n_scored = 0

    # -------------------------------------------------------- prediction

    def predict(self, x: np.ndarray, anchor: float,
                climatology_delta: float = 0.0) -> Dict[str, float]:
        learned_delta = self.model.predict(x)
        deltas = np.array([0.0, float(climatology_delta), float(learned_delta)])
        blended = float(np.dot(self.weights, deltas))
        mu = float(anchor + blended)
        sigma = self.model.predict_std(x, self.model.noise_var)
        lo, hi = self.conformal.interval(mu, fallback_sigma=sigma)
        return {
            "mu": mu,
            "lo": lo,
            "hi": hi,
            "sigma": sigma,
            "delta": blended,
            "members": {m: float(anchor + d) for m, d in zip(MEMBERS, deltas)},
            "weights": {m: float(w) for m, w in zip(MEMBERS, self.weights)},
        }

    # ---------------------------------------------------------- learning

    def learn(self, x: np.ndarray, anchor: float, truth: float,
              climatology_delta: float = 0.0) -> float:
        """One supervised step given a matured target."""
        deltas = np.array([0.0, float(climatology_delta),
                           float(self.model.predict(x))])
        member_pred = anchor + deltas
        losses = np.abs(member_pred - truth)

        # Hedge / exponentiated gradient on normalised losses
        scale = max(float(np.max(losses)), 1e-6)
        self.weights *= np.exp(-self.eta * losses / scale)
        self.weights = np.clip(self.weights, 1e-4, None)
        self.weights /= self.weights.sum()

        blended = float(np.dot(self.weights, member_pred))
        residual = truth - blended
        self.conformal.observe(residual)

        self.model.update(x, truth - anchor)

        self.member_mae = 0.98 * self.member_mae + 0.02 * losses
        self.n_scored += 1
        return residual

    def to_dict(self) -> Dict:
        return {"target": self.target, "horizon_s": self.horizon_s,
                "model": self.model.to_dict(), "conformal": self.conformal.to_dict(),
                "weights": self.weights.tolist(), "eta": self.eta,
                "member_mae": self.member_mae.tolist(), "n_scored": self.n_scored}

    @classmethod
    def from_dict(cls, s: Dict) -> "ForecastHead":
        h = cls(s["target"], s["horizon_s"])
        h.model = RecursiveLeastSquares.from_dict(s["model"])
        h.conformal = AdaptiveConformal.from_dict(s["conformal"])
        h.weights = np.array(s["weights"], dtype=float)
        h.eta = s["eta"]
        h.member_mae = np.array(s["member_mae"], dtype=float)
        h.n_scored = s.get("n_scored", 0)
        return h


class NowcastEnsemble:
    """The full bank of heads plus the shared feature standardiser."""

    def __init__(self, targets: Tuple[str, ...], horizons_s: Tuple[int, ...],
                 cfg_model):
        self.targets = tuple(targets)
        self.horizons = tuple(int(h) for h in horizons_s)
        self.grid_s = int(cfg_model.grid_s)
        self.scaler = Standardiser(N_FEATURES)
        self.heads: Dict[Tuple[str, int], ForecastHead] = {
            (t, h): ForecastHead(
                t, h, N_FEATURES, cfg_model.rls_forgetting, cfg_model.rls_delta,
                cfg_model.conformal_alpha, cfg_model.conformal_window,
                cfg_model.conformal_gamma,
            )
            for t in self.targets for h in self.horizons
        }
        self.trained_rows = 0

    # ------------------------------------------------------------ train

    def fit(self, X: np.ndarray, valid: np.ndarray, series: Dict[str, np.ndarray],
            climatology=None, grid_ts: Optional[np.ndarray] = None,
            passes: int = 1, max_pairs: int = 2500) -> Dict[str, int]:
        """Batch-update every head from history.

        `max_pairs` bounds the work per head to the most recent samples.
        This is not a shortcut: with a forgetting factor of 0.9985 the
        effective memory is about 11 hours, so the 4000th-most-recent
        sample carries a weight of roughly e^-6. Training on it costs
        real seconds on a Cortex-A53 and buys nothing measurable.
        """
        """Batch pass over history. Called on startup and every retrain tick."""
        if X.shape[0] < 10:
            return {"rows": 0}
        self.scaler.partial_fit(X[valid][:: max(1, X.shape[0] // 2000)])
        Xs = self.scaler.transform(X)

        counts = {}
        for target in self.targets:
            y = series[target]
            for h in self.horizons:
                steps = max(int(round(h / self.grid_s)), 1)
                Xa, dy, anchor = supervised_pairs(Xs, valid, y, steps)
                if Xa.shape[0] < 5:
                    counts[f"{target}@{h}"] = 0
                    continue
                if Xa.shape[0] > max_pairs:
                    Xa, dy, anchor = Xa[-max_pairs:], dy[-max_pairs:], anchor[-max_pairs:]
                head = self.heads[(target, h)]
                clim = np.zeros(Xa.shape[0])
                if climatology is not None and grid_ts is not None and climatology.ready:
                    n = grid_ts.size
                    ts_a = grid_ts[:n - steps]
                    mask_len = min(ts_a.size, Xa.shape[0])
                    clim_now = climatology.predict(target, ts_a[-mask_len:])
                    clim_fut = climatology.predict(target, ts_a[-mask_len:] + h)
                    clim = np.zeros(Xa.shape[0])
                    clim[-mask_len:] = clim_fut - clim_now
                for _ in range(max(int(passes), 1)):
                    for i in range(Xa.shape[0]):
                        head.learn(Xa[i], anchor[i], anchor[i] + dy[i], clim[i])
                counts[f"{target}@{h}"] = int(Xa.shape[0])
        self.trained_rows = int(X.shape[0])
        return counts

    # --------------------------------------------------------- inference

    def forecast(self, x_raw: np.ndarray, anchors: Dict[str, float], now: float,
                 climatology=None) -> Dict[str, Dict[int, Dict[str, float]]]:
        x = self.scaler.transform(np.atleast_2d(x_raw))[0]
        out: Dict[str, Dict[int, Dict[str, float]]] = {}
        for target in self.targets:
            anchor = float(anchors.get(target, 0.0))
            out[target] = {}
            for h in self.horizons:
                clim_delta = 0.0
                if climatology is not None and climatology.ready:
                    clim_delta = float(climatology.predict(target, np.array([now + h]))[0]
                                       - climatology.predict(target, np.array([now]))[0])
                out[target][h] = self.heads[(target, h)].predict(x, anchor, clim_delta)
        return out

    def diagnostics(self) -> List[Dict]:
        rows = []
        for (target, h), head in sorted(self.heads.items()):
            rows.append({
                "target": target,
                "horizon_s": h,
                "n_updates": head.model.n_updates,
                "n_scored": head.n_scored,
                "weights": {m: round(float(w), 3) for m, w in zip(MEMBERS, head.weights)},
                "member_mae": {m: round(float(v), 3) for m, v in zip(MEMBERS, head.member_mae)},
                "conformal_alpha": round(head.conformal.alpha, 4),
                "conformal_halfwidth": round(float(head.conformal.quantile()), 3)
                if np.isfinite(head.conformal.quantile()) else None,
                "coverage": round(head.conformal.empirical_coverage, 3)
                if np.isfinite(head.conformal.empirical_coverage) else None,
            })
        return rows

    def to_dict(self) -> Dict:
        return {
            "targets": list(self.targets),
            "horizons": list(self.horizons),
            "grid_s": self.grid_s,
            "scaler": self.scaler.to_dict(),
            "heads": [h.to_dict() for h in self.heads.values()],
            "trained_rows": self.trained_rows,
        }

    def load_dict(self, s: Dict) -> None:
        self.scaler = Standardiser.from_dict(s["scaler"])
        for hs in s["heads"]:
            head = ForecastHead.from_dict(hs)
            self.heads[(head.target, head.horizon_s)] = head
        self.trained_rows = s.get("trained_rows", 0)
