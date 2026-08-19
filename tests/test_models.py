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
    first_updates = head.model.n_updates

    norms = []
    for _ in range(15):
        ens.fit(X, valid, cols, None, ts)
        norms.append(float(np.linalg.norm(head.model.theta)))

    # Exact equality is no longer the right assertion: the stride rotates its
    # phase each refit, so a given refit trains on 12 or 13 pairs depending on
    # where the offset lands. One update of slack covers that. Sixteen passes
    # of accumulation would show up as 16x, not as 1.
    assert abs(head.model.n_updates - first_updates) <= 1, \
        "updates accumulated across refits; a refit must start from the prior"

    # The failure this guards against put ||theta|| at 1680 against a median
    # weight of 1.67. Phase rotation moves the norm by about 25% on these
    # deliberately signal-free features, so bound the magnitude rather than
    # pinning the value, and check it is not climbing refit on refit.
    assert max(norms) < 20.0, f"weights drifting without bound: {max(norms):.1f}"
    assert np.mean(norms[-5:]) < 3.0 * np.mean(norms[:5]), "weights growing across refits"


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


def test_training_pairs_are_strided_by_the_horizon():
    """Overlapping windows must not be counted as independent observations.

    At the 1 d horizon on a 5-minute grid adjacent pairs share 287 of their 288
    samples. Training on every row hands the filter the same outcome 288 times
    and RLS with forgetting reads each as fresh evidence, so a 400-score
    conformal window ends up holding 1.4 independent outcomes while believing
    it holds 400.
    """
    from ashvale.config import CONFIG
    from ashvale.models.nowcast import NowcastEnsemble

    rng = np.random.default_rng(11)
    n = 4000                                    # ~14 days at 5 minutes
    g = CONFIG.model.grid_s
    ts = np.arange(n) * g + 1.7554e9
    cols = {
        "temperature": 20 + 4 * np.sin(np.arange(n) / 288.0) + 0.1 * rng.normal(size=n),
        "humidity": 55 + 8 * np.cos(np.arange(n) / 288.0),
        "pressure": 1013 + 4 * np.sin(np.arange(n) / 900.0),
        "lux": np.clip(400 * np.sin(np.arange(n) / 288.0), 0, None),
    }
    X = rng.normal(size=(n, 33))
    X[:, 0] = 1.0
    valid = np.ones(n, dtype=bool)

    ens = NowcastEnsemble(CONFIG.model.targets, CONFIG.model.horizons_s, CONFIG.model)
    counts = ens.fit(X, valid, cols, None, ts)

    for h in CONFIG.model.horizons_s:
        steps = max(round(h / g), 1)
        got = counts[f"temperature@{h}"]
        # fit() bounds recency to max_pairs rows before it strides them.
        available = min(n - steps, 2500)
        expected = available // steps
        if expected >= CONFIG.model.min_pairs_per_head:
            assert abs(got - expected) <= 1, (
                f"horizon {h}s trained on {got} pairs, expected about {expected}")
            assert got < available / 2, "pairs were not strided"
        else:
            # The floor relaxes the stride rather than letting a long horizon
            # train on a handful of pairs.
            assert got >= CONFIG.model.min_pairs_per_head


def test_the_stride_floor_protects_a_short_record():
    """A 1 d horizon on two days of data must not train on two pairs."""
    from ashvale.config import CONFIG
    from ashvale.models.nowcast import NowcastEnsemble

    rng = np.random.default_rng(12)
    n = 700                                     # ~2.4 days at 5 minutes
    g = CONFIG.model.grid_s
    ts = np.arange(n) * g + 1.7554e9
    cols = {"temperature": 21 + rng.normal(size=n) * 0.1,
            "humidity": 50 + rng.normal(size=n) * 0.1,
            "pressure": 1013 + rng.normal(size=n) * 0.1,
            "lux": np.zeros(n)}
    X = rng.normal(size=(n, 33))
    X[:, 0] = 1.0
    ens = NowcastEnsemble(CONFIG.model.targets, CONFIG.model.horizons_s, CONFIG.model)
    counts = ens.fit(X, np.ones(n, dtype=bool), cols, None, ts)

    day = counts["temperature@86400"]
    assert day >= CONFIG.model.min_pairs_per_head, (
        f"1 d head trained on only {day} pairs; the floor did not engage")


def test_refit_phase_rotates_and_survives_serialisation():
    """Every offset must eventually be trained on, across restarts too."""
    from ashvale.config import CONFIG
    from ashvale.models.nowcast import NowcastEnsemble

    rng = np.random.default_rng(13)
    n = 600
    g = CONFIG.model.grid_s
    ts = np.arange(n) * g + 1.7554e9
    cols = {k: 20 + rng.normal(size=n) * 0.1 for k in CONFIG.model.targets}
    cols["lux"] = np.zeros(n)
    X = rng.normal(size=(n, 33))
    X[:, 0] = 1.0
    valid = np.ones(n, dtype=bool)

    ens = NowcastEnsemble(CONFIG.model.targets, CONFIG.model.horizons_s, CONFIG.model)
    assert ens.refit_phase == 0
    ens.fit(X, valid, cols, None, ts)
    ens.fit(X, valid, cols, None, ts)
    assert ens.refit_phase == 2

    back = NowcastEnsemble(CONFIG.model.targets, CONFIG.model.horizons_s, CONFIG.model)
    back.load_dict(ens.to_dict())
    assert back.refit_phase == 2, "a restart must not reset the stride to phase 0 forever"


def test_model_tuning_comes_from_config_not_from_the_state_file():
    """The same defect that made a Kalman retune silently do nothing.

    RecursiveLeastSquares.from_dict and AdaptiveConformal.from_dict restore
    lambda, delta, alpha, gamma and the conformal window alongside their data,
    and load_dict replaces the config-built heads with those. So every one of
    those knobs was immutable on any station that already had state: edit
    config.yaml, restart, and the old value comes straight back.
    """
    from ashvale.config import load_config
    from ashvale.models.nowcast import NowcastEnsemble

    cfg = load_config()
    old = cfg.model
    ens = NowcastEnsemble(old.targets, old.horizons_s, old)
    saved = ens.to_dict()

    import copy
    new = copy.deepcopy(old)
    new.rls_forgetting = 0.995
    new.rls_delta = 55.0
    new.conformal_alpha = 0.20
    new.conformal_gamma = 0.05
    new.conformal_window = 250

    back = NowcastEnsemble(new.targets, new.horizons_s, new)
    back.load_dict(saved)
    head = next(iter(back.heads.values()))
    assert head.model.lam == 0.995, "state file pinned the forgetting factor"
    assert head.model.delta == 55.0, "state file pinned the RLS prior"
    assert head.conformal.alpha_target == 0.20
    assert head.conformal.gamma == 0.05
    assert head.conformal.scores.maxlen == 250


def test_loading_state_ignores_heads_this_build_no_longer_has():
    """A stale state file must not resurrect a retired target or horizon."""
    from ashvale.config import load_config
    from ashvale.models.nowcast import NowcastEnsemble

    cfg = load_config().model
    ens = NowcastEnsemble(cfg.targets, cfg.horizons_s, cfg)
    saved = ens.to_dict()
    saved["heads"].append({**saved["heads"][0], "target": "retired_signal"})

    back = NowcastEnsemble(cfg.targets, cfg.horizons_s, cfg)
    back.load_dict(saved)
    assert not any(t == "retired_signal" for t, _ in back.heads)
    assert len(back.heads) == len(cfg.targets) * len(cfg.horizons_s)


def test_conformal_produces_a_band_from_the_fewest_scores_that_permit_one():
    """20 was arbitrary and became harmful once pairs were strided.

    The (1-alpha) empirical quantile is the ceil((k+1)(1-alpha))-th of k order
    statistics, so alpha = 0.10 needs k >= 9. Requiring 20 threw away a valid
    band at k = 13, which is roughly what a long-horizon head earns per refit
    after striding, and dropped twelve of eighteen heads onto 1.645*sigma with
    sigma from an unconstrained x'Px. That produced +/- 115% relative humidity.
    """
    from ashvale.models.rls import AdaptiveConformal

    assert AdaptiveConformal.MIN_SCORES == 9

    ac = AdaptiveConformal(alpha=0.10, gamma=0.01)
    for i in range(8):
        ac.observe(0.1 * (i + 1), True)
    assert not np.isfinite(ac.quantile()), "8 scores cannot support a 90% band"

    ac.observe(0.9, True)
    q = ac.quantile()
    assert np.isfinite(q), "9 scores must produce a band"
    assert q > 0


def test_strided_heads_do_not_fall_back_to_the_gaussian():
    """The end-to-end version of the same thing, through a real fit."""
    from ashvale.config import CONFIG
    from ashvale.models.climatology import HarmonicClimatology
    from ashvale.models.nowcast import NowcastEnsemble

    rng = np.random.default_rng(19)
    n = 700                                    # ~2.4 days, a young station
    g = CONFIG.model.grid_s
    ts = np.arange(n) * g + 1.7554e9
    cols = {
        "temperature": 22 + 3 * np.sin(np.arange(n) / 288.0) + 0.1 * rng.normal(size=n),
        "humidity": 50 + 9 * np.cos(np.arange(n) / 288.0),
        "pressure": 1013 + 2 * np.sin(np.arange(n) / 600.0),
        "lux": np.clip(400 * np.sin(np.arange(n) / 288.0), 0, None),
    }
    X = rng.normal(size=(n, 33))
    X[:, 0] = 1.0
    valid = np.ones(n, dtype=bool)

    clim = HarmonicClimatology(CONFIG.model.targets,
                               min_days_annual=CONFIG.model.climatology_min_days_annual)
    clim.fit(ts, {k: cols[k] for k in CONFIG.model.targets}, valid)
    ens = NowcastEnsemble(CONFIG.model.targets, CONFIG.model.horizons_s, CONFIG.model)
    ens.fit(X, valid, {k: cols[k] for k in CONFIG.model.targets}, clim, ts)

    for (target, h), head in ens.heads.items():
        q = head.conformal.quantile()
        assert np.isfinite(q), (
            f"{target}@{h}s has {len(head.conformal.scores)} scores and no band, "
            "so it falls back to the Gaussian")
        # and the band must be physical, not an unconstrained sigma
        limit = {"temperature": 25.0, "humidity": 60.0, "pressure": 40.0}[target]
        assert q < limit, f"{target}@{h}s band is +/- {q:.1f}, which is not a forecast"
