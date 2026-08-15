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
