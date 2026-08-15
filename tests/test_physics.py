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

"""Physics closed forms.

These are properties, not golden numbers. A golden number test tells you the
output changed; a property test tells you the output became unphysical, which
is the failure that actually matters here.
"""

from __future__ import annotations

import numpy as np
import pytest

from ashvale import physics


@pytest.mark.parametrize("t", [-20.0, -5.0, 0.0, 12.3, 25.0, 40.0])
def test_dew_point_at_saturation_equals_temperature(t):
    """100% RH means the air is already at its dew point."""
    assert float(physics.dew_point(t, 100.0)) == pytest.approx(t, abs=1e-6)


@pytest.mark.parametrize("t,rh", [(20.0, 50.0), (5.0, 80.0), (30.0, 20.0), (-3.0, 95.0)])
def test_dew_point_never_exceeds_temperature(t, rh):
    assert float(physics.dew_point(t, rh)) <= t + 1e-9


def test_dew_point_round_trip_through_vapour_pressure():
    """e(T, RH) evaluated at the dew point must be the saturation pressure."""
    for t, rh in [(20.0, 50.0), (25.6, 72.9), (0.5, 90.0)]:
        td = float(physics.dew_point(t, rh))
        assert float(physics.vapour_pressure(t, rh)) == pytest.approx(
            float(physics.saturation_vapour_pressure(td)), rel=1e-6)


def test_saturation_vapour_pressure_is_monotonic_in_temperature():
    t = np.linspace(-30.0, 50.0, 400)
    es = np.asarray(physics.saturation_vapour_pressure(t), dtype=float)
    assert np.all(np.diff(es) > 0.0)


@pytest.mark.parametrize("t,rh", [(20.0, 50.0), (30.0, 30.0), (10.0, 95.0)])
def test_wet_bulb_between_dew_point_and_temperature(t, rh):
    """The psychrometric ordering Td <= Tw <= T is not optional."""
    td = float(physics.dew_point(t, rh))
    tw = float(physics.wet_bulb(t, rh))
    assert td - 1e-6 <= tw <= t + 1e-6


def test_vpd_is_zero_at_saturation_and_positive_below():
    assert float(physics.vapour_pressure_deficit(20.0, 100.0)) == pytest.approx(0.0, abs=1e-9)
    assert float(physics.vapour_pressure_deficit(20.0, 40.0)) > 0.0


def test_sea_level_pressure_round_trips_with_station_pressure():
    for p, t, alt in [(1000.0, 15.0, 11.0), (1024.5, -2.0, 250.0), (985.0, 28.0, 0.0)]:
        slp = float(physics.sea_level_pressure(p, t, alt))
        back = float(physics.station_pressure(slp, t, alt))
        assert back == pytest.approx(p, rel=1e-9)


def test_sea_level_pressure_is_above_station_pressure_when_elevated():
    assert float(physics.sea_level_pressure(1000.0, 15.0, 100.0)) > 1000.0
    assert float(physics.sea_level_pressure(1000.0, 15.0, 0.0)) == pytest.approx(1000.0, rel=1e-12)


def test_solar_elevation_is_higher_at_local_noon_than_midnight():
    # 21 June 2026, Cambridge. Noon UTC against midnight UTC.
    noon, _ = physics.solar_position(np.array([1781784000.0]), 52.2053, 0.1218)
    midnight, _ = physics.solar_position(np.array([1781740800.0]), 52.2053, 0.1218)
    assert float(np.atleast_1d(noon)[0]) > float(np.atleast_1d(midnight)[0])


def test_clear_sky_irradiance_is_zero_below_the_horizon():
    assert float(np.atleast_1d(physics.clear_sky_irradiance(np.array([-10.0])))[0]) == 0.0
    assert float(np.atleast_1d(physics.clear_sky_irradiance(np.array([45.0])))[0]) > 0.0


def test_absolute_humidity_rises_with_temperature_at_fixed_rh():
    a = float(physics.absolute_humidity(10.0, 60.0))
    b = float(physics.absolute_humidity(25.0, 60.0))
    assert b > a
