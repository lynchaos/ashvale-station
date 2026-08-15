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

"""A structured account of what this station actually does, and why.

This module exists so the Methods page in the UI is generated from one
declarative source rather than hand-written HTML that drifts out of date
the first time someone changes a forgetting factor. Every parameter
quoted below is read from the live config at request time, so the page
describes the station you are running, not the one I shipped.

Each stage records what it consumes, what it produces, the technique, and
crucially a `why` and a `failure` field. The failure mode is the part
that usually goes undocumented and is the part you need at 2 a.m.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .features import FEATURE_NAMES
from .models.nowcast import MEMBERS


def pipeline(cfg) -> List[Dict[str, Any]]:
    m, s, site = cfg.model, cfg.sensor, cfg.site
    horizons = ", ".join(_fmt(h) for h in m.horizons_s)

    return [
        {
            "id": "acquire",
            "stage": "1",
            "title": "Acquisition",
            "module": "sensors.py",
            "technique": "Direct I2C, plus a stochastic simulator fallback",
            "consumes": "HTS221, LPS25HB, LSM9DS1, TCS3400, SoC thermal zone",
            "produces": "Raw multi-sensor sample every "
                        f"{s.sample_period_s:g} s",
            "why": "The colour sensor is read over raw smbus rather than through "
                   "the sense_hat library because the library does not expose the "
                   "TCS3400 clear channel, which is the one that carries the "
                   "cloudiness signal.",
            "failure": "If the sense_hat import fails the board silently becomes a "
                       "simulator. The dashboard header says so rather than "
                       "letting you trust synthetic weather.",
            "params": {"sample period": f"{s.sample_period_s:g} s",
                       "persist period": f"{s.persist_period_s:g} s"},
        },
        {
            "id": "compensate",
            "stage": "2",
            "title": "Self-heating compensation",
            "module": "estimation.py",
            "technique": "Grey-box model, coefficient by recursive least squares",
            "consumes": "T_raw, T_cpu, and any trusted reference you supply",
            "produces": "T = T_raw - k (T_cpu - T_raw)",
            "why": "The temperature and pressure sensors sit millimetres above a "
                   "SoC running 20 to 25 C hotter than the room. The usual fix "
                   "hard-codes k = 1/1.5, but k depends on your case, orientation, "
                   "airflow and CPU load. Here it is one estimated parameter with a "
                   "forgetting factor, updated from a single thermometer reading.",
            "failure": "A mistyped reference drives k to its clamp and stays there "
                       "across restarts, because state persists. The reset button "
                       "on the Models tab exists for exactly that.",
            "math": r"k_{t} = k_{t-1} + \frac{P\varphi}{\lambda + \varphi P \varphi}"
                    r"\left[(T_{raw} - T_{ref}) - k_{t-1}\varphi\right],"
                    r"\quad \varphi = T_{cpu} - T_{raw}",
            "params": {"current k": f"{s.cpu_heat_k:g} (prior)",
                       "clamp": f"{s.cpu_heat_k_min:g} to {s.cpu_heat_k_max:g}"},
        },
        {
            "id": "kalman",
            "stage": "3",
            "title": "State estimation",
            "module": "estimation.py",
            "technique": "Constant-velocity Kalman filter per signal, Joseph form",
            "consumes": "Compensated temperature, humidity, station pressure",
            "produces": "Filtered level and, more importantly, filtered rate",
            "why": "Pressure tendency is the single most informative variable a "
                   "point sensor can offer, and the LPS25HB noise floor makes a "
                   "naive finite difference pure noise. A CV filter estimates "
                   "level and rate jointly. The Joseph covariance update is used "
                   "because the standard form loses positive semi-definiteness "
                   "over months of continuous running.",
            "failure": "Process noise too low and the filter lags real weather; too "
                       "high and you have an expensive passthrough. The innovation "
                       "statistic is logged so you can tell which.",
            "math": r"x = \begin{bmatrix} \text{level} \\ \text{rate} \end{bmatrix},"
                    r"\quad Q = q\begin{bmatrix} \Delta t^3/3 & \Delta t^2/2 \\"
                    r"\Delta t^2/2 & \Delta t \end{bmatrix}",
            "params": {"q temperature": f"{s.kalman_q_temp:g}",
                       "r temperature": f"{s.kalman_r_temp:g}",
                       "q pressure": f"{s.kalman_q_press:g}"},
        },
        {
            "id": "features",
            "stage": "4",
            "title": "Feature construction",
            "module": "features.py, physics.py",
            "technique": f"{len(FEATURE_NAMES)} features on a {m.grid_s} s grid, "
                         "streaming z-scoring by Welford moments",
            "consumes": "Resampled history",
            "produces": "Design matrix, standardised",
            "why": "Three rules. Anything derivable from physics is computed, not "
                   "learned: dew point, wet bulb, VPD, solar elevation and a "
                   "clear-sky cloud index are closed-form, so making a learner "
                   "rediscover the Magnus curve from data wastes both samples and "
                   "capacity. Anything periodic is encoded as sine and cosine pairs "
                   "so a linear model can represent phase without a discontinuity "
                   "at midnight. Every lag is expressed in hours, not samples, so "
                   "changing the grid does not silently change meaning.",
            "failure": "Unstandardised features give a condition number that will "
                       "embarrass you: pressure sits near 1013 while temperature "
                       "rate sits near 0.02.",
            "params": {"grid": f"{m.grid_s} s", "features": str(len(FEATURE_NAMES)),
                       "site": f"{site.latitude:.3f}, {site.longitude:.3f} at "
                               f"{site.altitude_m:g} m"},
        },
        {
            "id": "nowcast",
            "stage": "5",
            "title": "Multi-horizon forecasting",
            "module": "models/nowcast.py, models/rls.py",
            "technique": f"{len(m.targets) * len(m.horizons_s)} direct heads, "
                         "exponentially weighted RLS, Hedge-blended",
            "consumes": "Design matrix and matured targets",
            "produces": f"Forecasts at {horizons} for {', '.join(m.targets)}",
            "why": "Direct heads, not one model iterated forward: iterating a "
                   "one-step model 288 times to reach 24 hours compounds its own "
                   "bias into a beautifully smooth lie. RLS rather than SGD because "
                   "a station makes only 288 grid rows a day and RLS is the exact "
                   "minimiser of the exponentially weighted squared error at every "
                   "step. Each head predicts a delta from now, never an absolute "
                   "level, so its capacity goes on the weather instead of the mean.",
            "failure": "Plain forgetting inflates the covariance exponentially "
                       "through quiet nights when the regressor barely moves, and "
                       "the model then detonates at sunrise. The trace is capped. "
                       "This is the most common way a field RLS deployment dies.",
            "math": r"P_t = \frac{1}{\lambda}\left(P_{t-1} - "
                    r"\frac{P_{t-1}x x^{\top}P_{t-1}}{\lambda + x^{\top}P_{t-1}x}"
                    r"\right)",
            "params": {"forgetting": f"{m.rls_forgetting:g}",
                       "effective memory": _memory(m.rls_forgetting, m.grid_s),
                       "members": ", ".join(MEMBERS)},
        },
        {
            "id": "conformal",
            "stage": "6",
            "title": "Calibrated uncertainty",
            "module": "models/rls.py",
            "technique": "Adaptive conformal inference",
            "consumes": "Realised forecast errors from the verification loop",
            "produces": f"{int((1 - m.conformal_alpha) * 100)}% prediction intervals",
            "why": "Split conformal is only valid under exchangeability, and "
                   "weather is emphatically not exchangeable: a front arrives and "
                   "yesterday's residual quantile becomes fiction. Adaptive "
                   "conformal feeds realised coverage back into the working alpha, "
                   "so the band widens after each miss and narrows after each hit. "
                   "Long-run coverage tracks the target whatever the distribution "
                   "does underneath.",
            "failure": "If coverage sits far from target, the feedback rate is "
                       "wrong, not the model. Both are shown on the Models tab.",
            "math": r"\alpha_{t+1} = \alpha_t + \gamma\left(\alpha^{*} - "
                    r"\mathbb{1}[y_t \notin C_t]\right)",
            "params": {"target coverage": f"{int((1 - m.conformal_alpha) * 100)}%",
                       "gamma": f"{m.conformal_gamma:g}",
                       "window": f"{m.conformal_window} residuals"},
        },
        {
            "id": "climatology",
            "stage": "7",
            "title": "Long-range outlook",
            "module": "models/climatology.py",
            "technique": "Ridge-regularised harmonic regression, anomaly decay",
            "consumes": "Full history",
            "produces": "Seven-day outlook with widening intervals",
            "why": "An honest statement: a single point sensor cannot see a front "
                   "approaching from the Atlantic. Beyond about twelve hours the "
                   "only information it holds is where you are in the diurnal and "
                   "annual cycles, the current pressure anomaly, and the local "
                   "trend. So that is exactly what this uses, and the API labels "
                   "the result an outlook rather than a forecast.",
            "failure": "Annual harmonics stay switched off below "
                       f"{m.climatology_min_days_annual:g} days of history. Fitting "
                       "a 365-day sine to three weeks of data produces a "
                       "magnificent extrapolation straight off the edge of the "
                       "physical world.",
            "math": r"y \sim \beta_0 + \beta_1 t + \sum_{k=1}^{3}"
                    r"\left[a_k\sin\tfrac{2\pi k t}{\text{day}} + "
                    r"b_k\cos\tfrac{2\pi k t}{\text{day}}\right] + \text{annual}",
            "params": {"diurnal harmonics": "3", "annual harmonics": "2",
                       "anomaly half-life": "30 h"},
        },
        {
            "id": "precip",
            "stage": "8",
            "title": "Precipitation",
            "module": "models/precip.py",
            "technique": "Zambretti prior, online logistic residual by AdaGrad",
            "consumes": "Sea-level pressure, tendency, humidity, cloud index, labels",
            "produces": "Condition class and rain probability",
            "why": "The 1915 Negretti and Zambra algorithm needs only pressure, its "
                   "tendency and the season. It has no parameters to overfit and "
                   "works from the first hour of deployment, so it is the prior. "
                   "The logistic layer learns only the residual: what your specific "
                   "location does that a slide rule cannot know. Its coefficient on "
                   "the Zambretti logit starts at exactly 1.0, so the model begins "
                   "as the slide rule and departs only where data insist.",
            "failure": "Labels are the bottleneck. Without a rain gauge the proxy "
                       "label abstains in the ambiguous middle rather than "
                       "guessing, because a poisoned training set costs more than "
                       "the extra samples buy. Trust grows as n/(n+25) in strong "
                       "labels, so the two buttons on the Forecast tab matter.",
            "params": {"prior": "Zambretti, three-branch",
                       "learner": "logistic, AdaGrad",
                       "strong label weight": "10x proxy"},
        },
        {
            "id": "monitor",
            "stage": "9",
            "title": "Monitoring",
            "module": "models/anomaly.py",
            "technique": "Mahalanobis EWMA, Page-Hinkley, latch detection",
            "consumes": "Filtered signals and matured forecast errors",
            "produces": "Novelty score, drift alarms, per-sensor health",
            "why": "Three detectors because they fail differently. Novelty catches "
                   "a window opening or a squall. Page-Hinkley catches the slow "
                   "stuff, a sensor drifting or a season turning, and it triggers "
                   "retraining, which is a far better signal than a cron schedule. "
                   "Latch detection catches the quietest failure of all: a sensor "
                   "that stops changing looks perfectly normal to both the others.",
            "failure": "With six signals the sample covariance is singular for the "
                       "first hour, and a singular covariance turns Mahalanobis "
                       "distance into a random number generator with an "
                       "authoritative name. Shrinkage toward a scaled identity is "
                       "not optional.",
            "params": {"novelty threshold": f"{m.anomaly_threshold:g}",
                       "EWMA lambda": f"{m.anomaly_ewma_lambda:g}",
                       "drift lambda": f"{m.drift_lambda:g}"},
        },
        {
            "id": "verify",
            "stage": "10",
            "title": "Verification",
            "module": "station.py",
            "technique": "Rolling scoring against persistence and climatology",
            "consumes": "Stored forecasts whose validity time has passed",
            "produces": "MAE, RMSE, bias, coverage, skill",
            "why": "This is the stage most projects skip and the one that makes the "
                   "difference. A forecast that is never scored is an opinion. A "
                   "forecast scored against persistence is a measurement. Skill is "
                   "1 - MAE/MAE_persistence, so a negative number is not a failure "
                   "of the exercise, it is the exercise working: ship persistence "
                   "at that horizon and stop pretending.",
            "failure": "Nothing scores until forecasts mature, so the 24 hour row "
                       "is empty on day one. That is the loop being honest.",
            "params": {"cadence": "every 5 minutes",
                       "baselines": "persistence, climatology"},
        },
    ]


def data_flow() -> List[Dict[str, str]]:
    """Edges of the wiring diagram, drawn by the Methods tab."""
    return [
        {"from": "acquire", "to": "compensate", "label": "T_raw, T_cpu"},
        {"from": "compensate", "to": "kalman", "label": "T corrected"},
        {"from": "kalman", "to": "features", "label": "level + rate"},
        {"from": "kalman", "to": "precip", "label": "dp/dt"},
        {"from": "kalman", "to": "monitor", "label": "signals"},
        {"from": "features", "to": "nowcast", "label": "design matrix"},
        {"from": "features", "to": "climatology", "label": "history"},
        {"from": "nowcast", "to": "conformal", "label": "point forecast"},
        {"from": "climatology", "to": "nowcast", "label": "member"},
        {"from": "conformal", "to": "verify", "label": "interval"},
        {"from": "verify", "to": "conformal", "label": "coverage feedback"},
        {"from": "verify", "to": "monitor", "label": "errors"},
        {"from": "monitor", "to": "nowcast", "label": "retrain trigger"},
        {"from": "precip", "to": "verify", "label": "labels"},
    ]


def glossary() -> List[Dict[str, str]]:
    return [
        {"term": "Skill",
         "definition": "1 - MAE/MAE_persistence. Zero means no better than "
                       "assuming nothing changes. Negative means worse than that, "
                       "which is useful information rather than an embarrassment."},
        {"term": "Coverage",
         "definition": "Fraction of observations that landed inside the prediction "
                       "interval. Should sit near the target. Far above means the "
                       "bands are lazily wide, far below means they lie."},
        {"term": "Forgetting factor",
         "definition": "Exponential weight on past samples. 0.999 on a 5-minute "
                       "grid remembers roughly a day; 0.99 remembers about two "
                       "hours and chases noise."},
        {"term": "Persistence",
         "definition": "The baseline forecast: tomorrow equals today. Beating it "
                       "over short horizons is genuinely hard, which is why it is "
                       "the honest thing to measure against."},
        {"term": "Pressure tendency",
         "definition": "Rate of change of sea-level pressure. Falling fast means "
                       "an approaching low. This is the only variable in the "
                       "station that sees beyond your walls."},
        {"term": "Dew point depression",
         "definition": "Air temperature minus dew point. Small and shrinking means "
                       "saturation, fog or rain. Large means dry air."},
    ]


def _fmt(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


def _memory(lam: float, grid_s: int) -> str:
    """Effective memory of an exponential forgetting factor, 1/(1-lambda) samples."""
    if lam >= 1.0:
        return "unbounded"
    samples = 1.0 / (1.0 - lam)
    hours = samples * grid_s / 3600.0
    return f"~{samples:.0f} samples ({hours:.1f} h)"


def describe(cfg) -> Dict[str, Any]:
    return {
        "pipeline": pipeline(cfg),
        "flow": data_flow(),
        "glossary": glossary(),
        "features": FEATURE_NAMES,
        "honest_limits": [
            "Indoors this forecasts your room, not the sky. Pressure is the "
            "exception because it passes through walls, which is exactly why the "
            "precipitation model runs on pressure tendency rather than indoor "
            "humidity.",
            "Days two to seven are climatology with an anomaly correction, not a "
            "forecast. The station physically cannot observe an approaching "
            "system.",
            "Without a rain gauge, precipitation labels come from you. The learner "
            "earns influence in proportion to how many you have supplied.",
            "Every number on the Models tab is measured on your own data, not "
            "quoted from a benchmark. If skill is negative at some horizon, that "
            "is what your station is actually doing.",
        ],
    }
