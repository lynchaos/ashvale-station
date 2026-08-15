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

"""Compensators and the Kalman bank.

The inverse-property tests here exist because getting that algebra wrong has
already cost this project twice: once on temperature, where a mismatched
simulator injected 1.2 C of phantom noise floor, and once on humidity, where
the correction ran the wrong way against a reference hygrometer.
"""

from __future__ import annotations

import numpy as np
import pytest

from ashvale.estimation import HumidityCompensator, KalmanCV, ThermalCompensator
from ashvale.physics import dew_point, saturation_vapour_pressure

# ---------------------------------------------------------------- thermal

def test_thermal_forward_model_is_the_exact_inverse_of_the_compensator():
    """T_raw = (T + k*T_cpu)/(1+k) must invert T = T_raw - k(T_cpu - T_raw)."""
    for k, t_true, t_cpu in [(0.55, 19.0, 40.0), (0.26, 24.4, 40.2), (1.0, 5.0, 30.0)]:
        c = ThermalCompensator(k0=k, k_min=0.0, k_max=2.0)
        t_raw = (t_true + k * t_cpu) / (1.0 + k)
        assert c.compensate(t_raw, t_cpu) == pytest.approx(t_true, abs=1e-9)


def test_thermal_calibration_moves_k_toward_the_truth():
    c = ThermalCompensator(k0=0.30, k_min=0.05, k_max=1.5)
    k_true, t_true, t_cpu = 0.62, 19.0, 41.0
    t_raw = (t_true + k_true * t_cpu) / (1.0 + k_true)
    before = abs(c.k - k_true)
    c.calibrate(t_raw, t_cpu, t_true)
    assert abs(c.k - k_true) < before


def test_thermal_clamp_survives_a_mistyped_reference():
    c = ThermalCompensator(k0=0.55, k_min=0.15, k_max=1.20)
    for _ in range(50):
        c.calibrate(25.0, 40.0, -300.0)      # absurd reference
    assert c.k_min <= c.k <= c.k_max


def test_thermal_compensation_is_a_noop_without_a_gradient():
    c = ThermalCompensator(k0=0.8)
    assert c.compensate(21.0, 21.0) == pytest.approx(21.0)
    # and never amplifies when the CPU is cooler than the sensor
    assert c.compensate(21.0, 15.0) == pytest.approx(21.0)


# ---------------------------------------------------------------- humidity

def test_humidity_psychrometric_round_trip():
    """The simulator's forward model must invert the compensator exactly."""
    rh_true, t_true, t_raw = 62.0, 19.0, 25.6
    rh_sensor = rh_true * float(saturation_vapour_pressure(t_true) /
                                saturation_vapour_pressure(t_raw))
    hc = HumidityCompensator(psychrometric=True)
    assert hc.compensate(rh_sensor, t_raw, t_true) == pytest.approx(rh_true, abs=1e-6)


def test_humidity_psychrometric_preserves_dew_point():
    """Vapour pressure is the conserved quantity, so dew point must not move."""
    rh_sensor, t_raw, t_true = 60.0, 25.6, 19.0
    hc = HumidityCompensator(psychrometric=True)
    out = hc.compensate(rh_sensor, t_raw, t_true)
    assert float(dew_point(t_true, out)) == pytest.approx(float(dew_point(t_raw, rh_sensor)),
                                                          abs=1e-6)


def test_humidity_psychrometric_disabled_by_default():
    hc = HumidityCompensator()
    assert hc.compensate(60.0, 25.6, 19.0) == pytest.approx(60.0)


def test_humidity_offset_converges_on_a_reference():
    """The measured case: board reads 75.35% where the truth is 50.4%."""
    hc = HumidityCompensator()
    errors = []
    for _ in range(6):
        hc.calibrate(75.35, 27.94, 24.86, 50.4)
        errors.append(abs(hc.compensate(75.35, 27.94, 24.86) - 50.4))
    assert errors[-1] < errors[0]
    assert errors[-1] < 0.5


def test_humidity_offset_is_clamped():
    hc = HumidityCompensator()
    for _ in range(50):
        hc.calibrate(50.0, 20.0, 20.0, 100.0)
    assert hc.off_min <= hc.offset <= hc.off_max


def test_humidity_output_stays_in_range():
    hc = HumidityCompensator(offset=30.0)
    assert 0.0 <= hc.compensate(95.0, 20.0, 20.0) <= 100.0
    hc2 = HumidityCompensator(offset=-30.0)
    assert 0.0 <= hc2.compensate(5.0, 20.0, 20.0) <= 100.0


def test_humidity_state_round_trips_through_dict():
    hc = HumidityCompensator(offset=-24.2, psychrometric=True)
    hc.calibrate(70.0, 25.0, 21.0, 50.0)
    back = HumidityCompensator.from_dict(hc.to_dict())
    assert back.offset == pytest.approx(hc.offset)
    assert back.psychrometric is hc.psychrometric
    assert back.n_calibrations == hc.n_calibrations


# ---------------------------------------------------------------- kalman

def test_kalman_covariance_stays_symmetric_and_psd():
    """Joseph form exists precisely so this holds over a long run."""
    kf = KalmanCV(q=1e-6, r=0.05)
    rng = np.random.default_rng(7)
    for _ in range(20000):
        kf.update(20.0 + 0.05 * rng.normal(), 2.0)
    P = np.asarray(kf.P, dtype=float)
    assert np.allclose(P, P.T, atol=1e-12)
    assert np.all(np.linalg.eigvalsh(P) > -1e-12)


def test_kalman_tracks_a_constant_and_reports_zero_rate():
    kf = KalmanCV(q=1e-8, r=0.01)
    for _ in range(2000):
        kf.update(15.0, 2.0)
    assert kf.level == pytest.approx(15.0, abs=1e-3)
    assert kf.rate == pytest.approx(0.0, abs=1e-5)


def test_kalman_recovers_a_known_ramp_rate():
    kf = KalmanCV(q=1e-4, r=0.01)
    true_rate = 0.5 / 3600.0          # 0.5 units per hour
    for i in range(6000):
        kf.update(10.0 + true_rate * i * 2.0, 2.0)
    assert kf.rate * 3600.0 == pytest.approx(0.5, rel=0.05)


def test_kalman_ignores_non_finite_measurements():
    kf = KalmanCV(q=1e-6, r=0.05)
    kf.update(20.0, 2.0)
    lvl_before = kf.level
    kf.update(float("nan"), 2.0)
    assert kf.level == pytest.approx(lvl_before)


def test_kalman_nis_is_near_one_when_noise_matches_the_model():
    """NIS is the honest self-check: consistent filter, NIS about 1."""
    r = 0.04
    kf = KalmanCV(q=1e-7, r=r)
    rng = np.random.default_rng(11)
    nis = []
    for i in range(4000):
        kf.update(18.0 + np.sqrt(r) * rng.normal(), 2.0)
        if i > 500:
            nis.append(kf.nis)
    assert 0.5 < float(np.mean(nis)) < 2.0


def test_kalman_state_round_trips_through_dict():
    kf = KalmanCV(q=1e-6, r=0.05)
    for _ in range(50):
        kf.update(12.0, 2.0)
    back = KalmanCV.from_dict(kf.to_dict())
    assert back.level == pytest.approx(kf.level)
    assert back.rate == pytest.approx(kf.rate)
