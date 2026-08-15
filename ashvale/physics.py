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

"""Physics that the model does not have to learn.

Every function here is a closed-form relationship that would otherwise
have to be discovered from data. Feeding a learner `dew point` instead of
making it infer the Magnus curve from (T, RH) is the cheapest accuracy
you will ever buy, especially on 512 MB of RAM.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np

MAGNUS_A = 17.625
MAGNUS_B = 243.04  # degrees C
P_STD = 1013.25    # hPa


def saturation_vapour_pressure(temp_c):
    """Tetens / Magnus saturation vapour pressure in hPa."""
    t = np.asarray(temp_c, dtype=float)
    return 6.112 * np.exp(MAGNUS_A * t / (MAGNUS_B + t))


def vapour_pressure(temp_c, rh_pct):
    return saturation_vapour_pressure(temp_c) * np.clip(np.asarray(rh_pct, float), 0.0, 100.0) / 100.0


def vapour_pressure_deficit(temp_c, rh_pct):
    """VPD in hPa. Bioprocess people know this one from headspace humidity control."""
    return saturation_vapour_pressure(temp_c) - vapour_pressure(temp_c, rh_pct)


def dew_point(temp_c, rh_pct):
    """Magnus-Tetens dew point in degrees C."""
    t = np.asarray(temp_c, dtype=float)
    rh = np.clip(np.asarray(rh_pct, dtype=float), 1e-3, 100.0)
    gamma = (MAGNUS_A * t) / (MAGNUS_B + t) + np.log(rh / 100.0)
    return (MAGNUS_B * gamma) / (MAGNUS_A - gamma)


def absolute_humidity(temp_c, rh_pct):
    """Water content in g/m^3 via the ideal gas law."""
    e = vapour_pressure(temp_c, rh_pct) * 100.0  # Pa
    t_k = np.asarray(temp_c, dtype=float) + 273.15
    return e / (461.5 * t_k) * 1000.0


def heat_index(temp_c, rh_pct):
    """Rothfusz apparent temperature, valid above roughly 26 C."""
    t = np.asarray(temp_c, dtype=float) * 9.0 / 5.0 + 32.0
    r = np.asarray(rh_pct, dtype=float)
    hi = (-42.379 + 2.04901523 * t + 10.14333127 * r - 0.22475541 * t * r
          - 6.83783e-3 * t ** 2 - 5.481717e-2 * r ** 2 + 1.22874e-3 * t ** 2 * r
          + 8.5282e-4 * t * r ** 2 - 1.99e-6 * t ** 2 * r ** 2)
    hi = np.where(t < 80.0, t, hi)
    return (hi - 32.0) * 5.0 / 9.0


def sea_level_pressure(press_hpa, temp_c, altitude_m):
    """Reduce station pressure to mean sea level (barometric formula).

    Without this, a 15 m elevation offset masquerades as a permanent
    low-pressure system and every rule-of-thumb forecaster gets it wrong.
    """
    p = np.asarray(press_hpa, dtype=float)
    t = np.asarray(temp_c, dtype=float)
    h = float(altitude_m)
    return p * (1.0 - (0.0065 * h) / (t + 0.0065 * h + 273.15)) ** -5.257


def station_pressure(slp_hpa, temp_c, altitude_m):
    p = np.asarray(slp_hpa, dtype=float)
    t = np.asarray(temp_c, dtype=float)
    h = float(altitude_m)
    return p * (1.0 - (0.0065 * h) / (t + 0.0065 * h + 273.15)) ** 5.257


# ---------------------------------------------------------------- solar

def _day_of_year(ts: float) -> float:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.timetuple().tm_yday + dt.hour / 24.0 + dt.minute / 1440.0


def solar_position(ts, latitude: float, longitude: float):
    """Return (elevation_deg, azimuth_deg) using the NOAA low-precision model.

    Accurate to a few tenths of a degree, which is far beyond what a
    diurnal-cycle feature needs, and costs about twenty flops.
    """
    ts_arr = np.atleast_1d(np.asarray(ts, dtype=float))
    doy = np.array([_day_of_year(float(t)) for t in ts_arr])
    frac_hour = np.array([
        datetime.fromtimestamp(float(t), tz=timezone.utc).hour
        + datetime.fromtimestamp(float(t), tz=timezone.utc).minute / 60.0
        + datetime.fromtimestamp(float(t), tz=timezone.utc).second / 3600.0
        for t in ts_arr
    ])

    gamma = 2.0 * math.pi / 365.0 * (doy - 1.0)
    eqtime = 229.18 * (0.000075 + 0.001868 * np.cos(gamma) - 0.032077 * np.sin(gamma)
                       - 0.014615 * np.cos(2 * gamma) - 0.040849 * np.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * np.cos(gamma) + 0.070257 * np.sin(gamma)
            - 0.006758 * np.cos(2 * gamma) + 0.000907 * np.sin(2 * gamma)
            - 0.002697 * np.cos(3 * gamma) + 0.00148 * np.sin(3 * gamma))

    true_solar_min = frac_hour * 60.0 + eqtime + 4.0 * longitude
    hour_angle = np.radians(true_solar_min / 4.0 - 180.0)

    lat = math.radians(latitude)
    cos_zenith = (np.sin(lat) * np.sin(decl)
                  + np.cos(lat) * np.cos(decl) * np.cos(hour_angle))
    cos_zenith = np.clip(cos_zenith, -1.0, 1.0)
    elevation = np.degrees(np.arcsin(cos_zenith))

    azimuth = np.degrees(np.arctan2(
        -np.sin(hour_angle),
        np.tan(decl) * np.cos(lat) - np.sin(lat) * np.cos(hour_angle)
    )) % 360.0

    if np.isscalar(ts) or np.asarray(ts).ndim == 0:
        return float(elevation[0]), float(azimuth[0])
    return elevation, azimuth


def clear_sky_irradiance(elevation_deg):
    """Rough clear-sky global horizontal irradiance, W/m^2.

    Used as the denominator of a `cloudiness proxy` when the TCS3400 sees
    daylight: measured_lux / expected_lux is a surprisingly decent
    okta estimate through a south-facing window.
    """
    el = np.clip(np.asarray(elevation_deg, dtype=float), 0.0, 90.0)
    sin_el = np.sin(np.radians(el))
    air_mass = np.where(el > 0.5, 1.0 / np.maximum(sin_el, 1e-3), 40.0)
    return np.where(el > 0.0, 1353.0 * 0.7 ** (air_mass ** 0.678) * sin_el, 0.0)


def wet_bulb(temp_c, rh_pct):
    """Stull's empirical wet-bulb approximation, degrees C."""
    t = np.asarray(temp_c, dtype=float)
    rh = np.clip(np.asarray(rh_pct, dtype=float), 5.0, 99.0)
    return (t * np.arctan(0.151977 * np.sqrt(rh + 8.313659))
            + np.arctan(t + rh) - np.arctan(rh - 1.676331)
            + 0.00391838 * rh ** 1.5 * np.arctan(0.023101 * rh) - 4.686035)
