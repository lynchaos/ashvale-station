#!/usr/bin/env python3
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

"""Rolling-origin backtest. The only number that decides whether to ship.

Protocol, strictly walk-forward:

  1. Build the 5-minute feature grid from stored telemetry.
  2. Split at `--train-frac`. Fit the ensemble and the climatology on the
     first part only.
  3. Walk the second part one step at a time. At each step, forecast,
     record the error, and only then let the model learn from the target
     that has just matured. No target is ever visible before its time.
  4. Report MAE against three baselines:
        persistence  the value now
        climatology  the harmonic fit
        the ensemble

Skill = 1 - MAE_model / MAE_persistence. A positive number means the
model earns its electricity. A negative number at a given horizon is not
a failure of the exercise, it is the exercise working: ship persistence
at that horizon and stop pretending.

    python scripts/evaluate.py --train-frac 0.6
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ashvale.config import load_config                    # noqa: E402
from ashvale.features import build_features               # noqa: E402
from ashvale.models.climatology import HarmonicClimatology  # noqa: E402
from ashvale.models.nowcast import NowcastEnsemble        # noqa: E402
from ashvale.storage import Store, resample               # noqa: E402


def horizon_label(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--train-frac", type=float, default=0.6)
    ap.add_argument("--hours", type=float, default=24 * 60)
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    store = Store(cfg.storage.db_path)

    raw = store.window(args.hours, ["ts", "temp_smooth", "hum_smooth", "press_slp", "lux"])
    if raw["ts"].size < 200:
        print("Not enough history. Run: python scripts/simulate.py --days 14")
        return

    grid_ts, cols = resample(
        raw["ts"],
        {"temperature": raw["temp_smooth"], "humidity": raw["hum_smooth"],
         "pressure": raw["press_slp"], "lux": raw["lux"]},
        cfg.model.grid_s,
    )
    X, valid = build_features(grid_ts, cols["temperature"], cols["humidity"],
                              cols["pressure"], cols["lux"], cfg.model.grid_s,
                              cfg.site.latitude, cfg.site.longitude)

    n = grid_ts.size
    split = int(n * args.train_frac)
    span_days = (grid_ts[-1] - grid_ts[0]) / 86400.0
    print(f"grid rows      : {n}  ({span_days:.2f} days at {cfg.model.grid_s}s)")
    print(f"train / test   : {split} / {n - split}")

    clim = HarmonicClimatology(cfg.model.targets,
                               min_days_annual=cfg.model.climatology_min_days_annual)
    clim.fit(grid_ts[:split], {k: v[:split] for k, v in cols.items() if k in cfg.model.targets},
             valid[:split])

    ens = NowcastEnsemble(cfg.model.targets, cfg.model.horizons_s, cfg.model)
    t0 = time.time()
    ens.fit(X[:split], valid[:split],
            {k: v[:split] for k, v in cols.items() if k in cfg.model.targets},
            clim, grid_ts[:split])
    print(f"fit            : {time.time() - t0:.1f}s\n")

    per_step = cfg.model.grid_s
    results = {}

    for target in cfg.model.targets:
        y = cols[target]
        for h in cfg.model.horizons_s:
            steps = max(int(round(h / per_step)), 1)
            errs, pers, clims, covered = [], [], [], []
            head = ens.heads[(target, h)]

            for i in range(split, n - steps):
                if not valid[i] or not np.isfinite(y[i]) or not np.isfinite(y[i + steps]):
                    continue
                x = ens.scaler.transform(X[i:i + 1])[0]
                anchor = float(y[i])
                truth = float(y[i + steps])
                cd = 0.0
                if clim.ready:
                    cd = float(clim.predict(target, np.array([grid_ts[i] + h]))[0]
                               - clim.predict(target, np.array([grid_ts[i]]))[0])
                pred = head.predict(x, anchor, cd)
                errs.append(truth - pred["mu"])
                pers.append(truth - anchor)
                clims.append(truth - (anchor + cd))
                covered.append(1.0 if pred["lo"] <= truth <= pred["hi"] else 0.0)
                head.learn(x, anchor, truth, cd)      # learn only after scoring

            if len(errs) < 5:
                continue
            e = np.abs(errs)
            p = np.abs(pers)
            c = np.abs(clims)
            results[(target, h)] = {
                "mae": e.mean(), "persistence": p.mean(), "climatology": c.mean(),
                "skill": 1.0 - e.mean() / max(p.mean(), 1e-9),
                "bias": float(np.mean(errs)),
                "coverage": float(np.mean(covered)),
                "n": len(errs),
                "weights": {k: round(float(v), 2) for k, v in
                            zip(("pers", "clim", "rls"), head.weights)},
            }

    units = {"temperature": "C", "humidity": "%", "pressure": "hPa"}
    header = f"{'target':<12}{'lead':>6}{'MAE':>9}{'persist':>9}{'clim':>9}{'skill':>8}{'cover':>7}{'bias':>8}   weights"
    print(header)
    print("-" * len(header))
    for target in cfg.model.targets:
        for h in cfg.model.horizons_s:
            r = results.get((target, h))
            if not r:
                continue
            flag = "  <-- persistence wins" if r["skill"] < 0 else ""
            print(f"{target:<12}{horizon_label(h):>6}{r['mae']:>9.3f}{r['persistence']:>9.3f}"
                  f"{r['climatology']:>9.3f}{r['skill'] * 100:>7.1f}%{r['coverage'] * 100:>6.0f}%"
                  f"{r['bias']:>+8.3f}   {r['weights']}{flag}")
        print()

    print(f"units: temperature C, humidity %, pressure hPa")
    print("coverage should sit near 90% if the conformal calibration is honest.")


if __name__ == "__main__":
    main()
