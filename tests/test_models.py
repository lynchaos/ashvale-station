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

"""The learners: RLS, adaptive conformal, and the Zambretti prior.

The covariance-cap test is the important one in this file. Unbounded P growth
through an unexcited subspace is the most common way a field RLS deployment
dies, and it dies silently until the first excited sample.
"""

from __future__ import annotations

import numpy as np
import pytest

from ashvale.models.precip import zambretti
from ashvale.models.rls import AdaptiveConformal, RecursiveLeastSquares

# ---------------------------------------------------------------- RLS

def test_rls_recovers_known_coefficients():
    rng = np.random.default_rng(3)
    truth = np.array([0.5, -1.25, 2.0, 0.0])
    m = RecursiveLeastSquares(n_features=4, forgetting=0.999)
    for _ in range(4000):
        x = rng.normal(size=4)
        m.update(x, float(truth @ x))
    assert np.allclose(m.theta, truth, atol=0.02)


def test_rls_covariance_trace_never_exceeds_the_cap():
    """A quiet regressor is exactly what inflates P. It must not run away."""
    m = RecursiveLeastSquares(n_features=8, forgetting=0.99, p_max=1e4)
    quiet = np.zeros(8)
    quiet[0] = 1.0                      # only one direction ever excited
    for _ in range(50000):
        m.update(quiet, 1.0)
    tr = float(np.trace(np.asarray(m.P, dtype=float)))
    assert np.isfinite(tr)
    assert tr <= 1e4 * (1.0 + 1e-6)


def test_rls_covariance_stays_symmetric():
    rng = np.random.default_rng(5)
    m = RecursiveLeastSquares(n_features=6, forgetting=0.995)
    for _ in range(5000):
        m.update(rng.normal(size=6), float(rng.normal()))
    P = np.asarray(m.P, dtype=float)
    assert np.allclose(P, P.T, atol=1e-9)


def test_rls_survives_a_non_finite_sample_without_poisoning_theta():
    m = RecursiveLeastSquares(n_features=3, forgetting=0.99)
    for _ in range(100):
        m.update(np.array([1.0, 0.5, -0.2]), 1.0)
    good = m.theta.copy()
    m.update(np.array([np.nan, 1.0, 1.0]), 1.0)
    assert np.all(np.isfinite(m.theta)), "a NaN sample must not poison the weights"
    m.update(np.array([1.0, 1.0, 1.0]), float("inf"))
    assert np.all(np.isfinite(m.theta))
    assert good.shape == m.theta.shape


def test_rls_forgetting_gives_the_documented_effective_memory():
    m = RecursiveLeastSquares(n_features=2, forgetting=0.9985)
    assert 1.0 / (1.0 - m.lam) == pytest.approx(666.67, rel=1e-3)


# ---------------------------------------------------------------- conformal

def test_conformal_coverage_tracks_the_target_on_stationary_noise():
    ac = AdaptiveConformal(alpha=0.1, gamma=0.02)
    rng = np.random.default_rng(17)
    inside = 0
    n = 4000
    for i in range(n):
        err = float(rng.normal())
        q = float(ac.quantile())
        covered = bool(np.isfinite(q) and abs(err) <= q)
        if i > 400 and covered:
            inside += 1
        ac.observe(err, covered)
    assert 0.84 <= inside / (n - 400) <= 0.96


def test_conformal_alpha_is_clamped():
    ac = AdaptiveConformal(alpha=0.1, gamma=0.2)
    for _ in range(5000):
        ac.observe(1e9, False)  # always a miss, alpha should rise then stop
    assert 0.005 <= ac.alpha <= 0.75


def test_conformal_widens_after_misses_and_narrows_after_hits():
    """Mind the sign. The update is

        alpha <- alpha + gamma * (alpha_target - 1[miss])

    so a hit adds +gamma*alpha_target and a miss subtracts gamma*(1-alpha_target).
    Since the band is the (1-alpha) quantile, a *rising* alpha is a *narrowing*
    band. Hits therefore push alpha up and misses push it down, which reads
    backwards until you follow it through.
    """
    ac = AdaptiveConformal(alpha=0.1, gamma=0.05)
    for _ in range(200):
        ac.observe(0.1, True)
    a_hits = ac.alpha
    assert a_hits > 0.1, "a run of hits should raise alpha, narrowing the band"

    for _ in range(200):
        ac.observe(1e6, False)
    assert ac.alpha < a_hits, "a run of misses should lower alpha, widening the band"


# ---------------------------------------------------------------- zambretti

def test_zambretti_ordering_rising_is_never_worse_than_falling():
    """Z increases toward bad weather, so falling must not score below rising."""
    for p in [980.0, 1000.0, 1013.0, 1030.0]:
        rising = zambretti(p, +1.2, 6)["z"]
        steady = zambretti(p, 0.0, 6)["z"]
        falling = zambretti(p, -1.2, 6)["z"]
        assert rising <= steady <= falling, f"ordering broken at {p} hPa"


def test_zambretti_z_decreases_with_pressure_within_a_branch():
    for tend in (-1.2, 0.0, 1.2):
        zs = [zambretti(p, tend, 6)["z"] for p in (985.0, 1000.0, 1015.0, 1030.0)]
        assert all(a >= b for a, b in zip(zs, zs[1:])), f"not monotonic for tend={tend}"


def test_zambretti_stays_on_the_26_point_scale():
    for p in (940.0, 1050.0):
        for tend in (-5.0, 0.0, 5.0):
            assert 1 <= zambretti(p, tend, 6)["z"] <= 26


def test_zambretti_rain_prior_rises_with_z():
    settled = zambretti(1035.0, 1.5, 6)
    stormy = zambretti(960.0, -2.5, 6)
    assert stormy["prior_rain_prob"] > settled["prior_rain_prob"]


# ---------------------------------------------------------------- refit safety

def test_rls_reset_returns_to_the_prior():
    m = RecursiveLeastSquares(n_features=5, forgetting=0.999, delta=100.0)
    rng = np.random.default_rng(9)
    for _ in range(500):
        m.update(rng.normal(size=5), float(rng.normal()))
    assert m.n_updates == 500
    m.reset()
    assert m.n_updates == 0
    assert np.allclose(m.theta, 0.0)
    assert np.allclose(m.P, np.eye(5) * 100.0)


def test_rls_delta_survives_serialisation():
    """A refit after a restart must return to the same prior it started from."""
    m = RecursiveLeastSquares(n_features=4, forgetting=0.99, delta=100.0)
    m.update(np.ones(4), 1.0)
    back = RecursiveLeastSquares.from_dict(m.to_dict())
    back.reset()
    assert np.allclose(back.P, np.eye(4) * 100.0), "reload lost the prior"


def test_repeated_refits_do_not_accumulate():
    """Refitting the same history must be idempotent, not cumulative.

    This is the bug that put a 53 C six-hour forecast on a real station in a
    24 C room. fit() replayed history into a live filter on every retrain tick
    and never reset, so 453 grid rows had produced 64,676 updates in a day and a
    half. RLS with forgetting reads each update as fresh evidence, so P
    collapsed and the weights drifted without bound in the directions the data
    never excited.
    """
    from ashvale.config import CONFIG
    from ashvale.models.nowcast import NowcastEnsemble

    rng = np.random.default_rng(3)
    n = 400
    g = CONFIG.model.grid_s
    ts = np.arange(n) * g + 1.7554e9
    cols = {
        "temperature": 22 + 2 * np.sin(np.arange(n) / 40.0) + 0.2 * rng.normal(size=n),
        "humidity": 50 + 5 * np.cos(np.arange(n) / 33.0),
        "pressure": 1013 + np.sin(np.arange(n) / 77.0),
        "lux": np.clip(300 * np.sin(np.arange(n) / 120.0), 0, None),
    }
    X = rng.normal(size=(n, 33))
    X[:, 0] = 1.0
    valid = np.ones(n, dtype=bool)

    ens = NowcastEnsemble(CONFIG.model.targets, CONFIG.model.horizons_s, CONFIG.model)
    head = ens.heads[("temperature", 21600)]

    ens.fit(X, valid, cols, None, ts)
    first_norm = float(np.linalg.norm(head.model.theta))
    first_updates = head.model.n_updates

    for _ in range(15):
        ens.fit(X, valid, cols, None, ts)

    assert head.model.n_updates == first_updates, "updates accumulated across refits"
    assert float(np.linalg.norm(head.model.theta)) == pytest.approx(first_norm, rel=0.05)


def test_annual_harmonics_are_zero_until_the_record_spans_a_season():
    """Two near-constant, near-collinear columns are a rank-deficient regressor.

    Left on from day one, sin_doy and cos_doy carried +1174 and +1191 on a real
    station whose median weight was 1.67. Zero is the honest value: a day and a
    half of data says nothing whatsoever about the season.
    """
    from ashvale.features import FEATURE_NAMES, build_features

    n = 450
    ts = np.arange(n) * 300.0 + 1.7554e9          # about 1.5 days
    t = 22 + 2 * np.sin(np.arange(n) / 40.0)
    h = 50 + 5 * np.cos(np.arange(n) / 33.0)
    p = 1013 + np.sin(np.arange(n) / 77.0)
    lux = np.clip(300 * np.sin(np.arange(n) / 120.0), 0, None)

    si, ci = FEATURE_NAMES.index("sin_doy"), FEATURE_NAMES.index("cos_doy")

    X, _ = build_features(ts, t, h, p, lux, 300, 52.2, 0.12, min_days_annual=120.0)
    assert np.all(X[:, si] == 0.0) and np.all(X[:, ci] == 0.0)

    # A record that does span the year keeps them.
    ts_long = np.arange(n) * (200 * 86400.0 / n) + 1.7554e9
    X2, _ = build_features(ts_long, t, h, p, lux, 300, 52.2, 0.12, min_days_annual=120.0)
    assert X2[:, si].std() > 0.1, "annual terms should return once the record is long enough"
