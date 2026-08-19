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

"""Seed the database with synthetic history.

Why this exists: a freshly flashed Pi has no history, and a forecaster
with no history is a random number generator with a nice dashboard. This
script writes physically plausible past telemetry so you can exercise
training, verification and the whole dashboard before the real station
has logged its first night.

The generator is not a toy. It is a three-component stochastic model:

    pressure     Ornstein-Uhlenbeck, tau = 30 h, sigma = 9 hPa
                 (roughly the observed synoptic variability of NW Europe)
    temperature  seasonal harmonic + solar-driven diurnal cycle
                 + OU anomaly (tau = 6 h), with a nocturnal inversion term
    humidity     driven inversely by temperature about a dew point that
                 itself performs a slow random walk, which is what makes
                 RH and T correlate the way they actually do

Everything is then pushed through the same CPU-heating and noise model
the real sensor suffers from, so a model trained here does not fall over
when it meets real data.

    python scripts/simulate.py --days 21 --wipe
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ashvale.config import load_config  # noqa: E402
from ashvale.estimation import SignalTracker  # noqa: E402
from ashvale.physics import (  # noqa: E402
    clear_sky_irradiance,
    dew_point,
    sea_level_pressure,
    solar_position,
)
from ashvale.sensors import (  # noqa: E402
    K_HTS221,
    K_LPS25HB,
    SD_HTS221,
    SD_LPS25HB,
)
from ashvale.storage import Store  # noqa: E402


def generate(days: float, step_s: int, lat: float, lon: float,
             seed: int = 11, end: float | None = None,
             psychrometric: bool = False) -> dict:
    rng = np.random.default_rng(seed)
    n = int(days * 86400 / step_s)
    # Anchoring to wall clock makes a fixed seed insufficient for reproducibility:
    # the OU realisation repeats, but the timestamps shift, which moves solar
    # elevation, day of year and the seasonal harmonic. Those feed the temperature
    # model directly, so two same-seed runs produce different data. Pin end as well
    # and the backfill becomes bit-reproducible, which is what before/after
    # evidence on a model change actually requires.
    end = time.time() if end is None else end
    ts = end - np.arange(n)[::-1] * step_s

    # --- synoptic pressure: OU process
    tau_p, sigma_p = 30 * 3600.0, 9.0
    press = np.zeros(n)
    a = math.exp(-step_s / tau_p)
    noise_scale = sigma_p * math.sqrt(1 - a * a)
    for i in range(1, n):
        press[i] = a * press[i - 1] + noise_scale * rng.normal()
    press_slp = 1013.0 + press

    # --- solar forcing
    elev, _ = solar_position(ts, lat, lon)
    elev = np.atleast_1d(elev)
    ghi = clear_sky_irradiance(elev)
    cloud = np.clip(0.45 + 0.35 * np.sin(2 * np.pi * ts / (4.5 * 86400)) +
                    0.25 * rng.normal(size=n).cumsum() / math.sqrt(n), 0.0, 1.0)
    lux = np.maximum(ghi * 45.0 * (1.0 - 0.85 * cloud), 0.0) + 6.0

    # --- temperature: season + diurnal + OU anomaly + inversion at night
    doy = np.array([time.gmtime(float(t)).tm_yday for t in ts])
    seasonal = 6.5 * np.sin(2 * np.pi * (doy - 105) / 365.25)
    diurnal = 0.011 * ghi * (1.0 - 0.6 * cloud)
    inversion = -1.8 * (elev < -3).astype(float) * (1.0 - cloud)

    tau_t, sigma_t = 6 * 3600.0, 1.9
    at = math.exp(-step_s / tau_t)
    anom = np.zeros(n)
    for i in range(1, n):
        anom[i] = at * anom[i - 1] + sigma_t * math.sqrt(1 - at * at) * rng.normal()
    # pressure and temperature anomalies are correlated in the real world
    anom += 0.12 * press

    temp = 11.5 + seasonal + diurnal + inversion + anom

    # --- humidity via a slowly wandering dew point
    dew = temp - 4.5 + 2.5 * np.sin(2 * np.pi * ts / (3.2 * 86400))
    dew -= 0.10 * press
    dew = np.minimum(dew, temp - 0.2)
    es_t = 6.112 * np.exp(17.625 * temp / (243.04 + temp))
    es_d = 6.112 * np.exp(17.625 * dew / (243.04 + dew))
    rh = np.clip(100.0 * es_d / es_t, 8.0, 100.0)

    # CPU temperature: a slow AR(1) load process, not white noise. A Zero 2 W
    # under a steady FastAPI load drifts by a degree or two over minutes, it
    # does not jitter by four degrees between samples.
    cpu_load = np.zeros(n)
    a_cpu = math.exp(-step_s / (900.0))
    for i in range(1, n):
        cpu_load[i] = a_cpu * cpu_load[i - 1] + 1.6 * math.sqrt(1 - a_cpu * a_cpu) * rng.normal()
    cpu = temp + 21.0 + cpu_load

    # The sensor sits in a thermal gradient between the room and the SoC.
    # The compensator inverts  T = T_raw - k (T_cpu - T_raw),  so the forward
    # model must be its exact inverse:  T_raw = (T + k T_cpu) / (1 + k).
    # Generating it any other way bakes a bias into the synthetic data that
    # no amount of calibration can remove, and quietly caps your skill score.
    # Two thermometers, not one, because the board has two. Their forward
    # models average to the k = 0.55 case this used to generate directly, so
    # temp_raw is unchanged in expectation. Its noise is not: a real board
    # averages sd 0.060 with sd 0.443 and lands at 0.223, where this used to
    # claim 0.05. Simulating the quiet sensor and calling it the average is
    # what let an over-optimistic measurement noise go unnoticed.
    temp_h = (temp + K_HTS221 * cpu) / (1.0 + K_HTS221) + SD_HTS221 * rng.normal(size=n)
    temp_p = (temp + K_LPS25HB * cpu) / (1.0 + K_LPS25HB) + SD_LPS25HB * rng.normal(size=n)
    temp_raw = (temp_h + temp_p) / 2.0
    # If the compensator will move RH from the element temperature onto the air
    # temperature, the forward model here must be its exact inverse, or the
    # synthetic data bakes in a bias no calibration can remove. Same trap as the
    # thermal algebra above. Off by default, matching sensor.hum_psychrometric.
    if psychrometric:
        es_raw = 6.112 * np.exp(17.625 * temp_raw / (243.04 + temp_raw))
        rh_sensor = np.clip(rh * es_t / es_raw, 0.0, 100.0)
    else:
        rh_sensor = rh

    press_station = press_slp / (1.0 + 0.0) - 1.8      # nominal 15 m offset
    press_station += 0.05 * rng.normal(size=n)

    return {
        "ts": ts, "temp": temp, "temp_raw": temp_raw,
        "temp_h": temp_h, "temp_p": temp_p,
        "rh": rh_sensor + 0.4 * rng.normal(size=n),
        "press": press_station, "press_slp": press_slp, "cpu": cpu,
        "lux": lux * (0.85 + 0.3 * rng.random(n)), "dew": dew, "cloud": cloud,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=14.0)
    ap.add_argument("--step", type=int, default=300, help="seconds between rows")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--end", type=float, default=None,
                    help="unix timestamp the history ends at; defaults to now. "
                         "Pin it with --seed for a bit-reproducible backfill")
    ap.add_argument("--wipe", action="store_true", help="clear existing telemetry first")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    store = Store(cfg.storage.db_path)

    # The Kalman process noise is tuned for the real sampling cadence (2 s).
    # Backfilling at 300 s steps with the same q gives Q_level = q*dt^3/3, which
    # is five orders of magnitude larger, so the filter abandons smoothing and
    # tracks measurement noise. Its rate estimates then blow past anything
    # physical and poison the all-time records. Scale q by (real_dt/step)^3 so
    # the synthetic history has the same effective smoothing as the live station.
    scale = (cfg.sensor.sample_period_s / float(args.step)) ** 3
    cfg.sensor.kalman_q_temp *= scale
    cfg.sensor.kalman_q_hum *= scale
    cfg.sensor.kalman_q_press *= scale

    if args.wipe:
        with store._conn() as conn:
            conn.execute("DELETE FROM telemetry")
            conn.execute("DELETE FROM forecasts")
            conn.execute("DELETE FROM scores")
        print("cleared existing telemetry, forecasts and scores")

    data = generate(args.days, args.step, cfg.site.latitude, cfg.site.longitude,
                    args.seed, args.end, cfg.sensor.hum_psychrometric)
    tracker = SignalTracker(cfg)

    n = data["ts"].size
    t0 = time.time()
    for i in range(n):
        ts = float(data["ts"][i])
        est = tracker.step(ts, float(data["temp_raw"][i]), float(data["rh"][i]),
                           float(data["press"][i]), float(data["cpu"][i]))
        slp = float(sea_level_pressure(est["press_smooth"], est["temp_smooth"],
                                       cfg.site.altitude_m))
        store.insert_telemetry({
            "ts": ts,
            "temp_raw": data["temp_raw"][i],
            "temp_h": data["temp_h"][i],
            "temp_p": data["temp_p"][i],
            "temp_c": est["temp_c"],
            "temp_smooth": est["temp_smooth"],
            "temp_rate": est["temp_rate"],
            "hum": data["rh"][i],
            "hum_smooth": est["hum_smooth"],
            "press": data["press"][i],
            "press_slp": slp,
            "press_smooth": est["press_smooth"],
            "press_rate": est["press_rate"],
            "cpu_temp": data["cpu"][i],
            "dew_c": float(dew_point(est["temp_smooth"], est["hum_smooth"])),
            "lux": data["lux"][i],
            "r": data["lux"][i] * 0.30, "g": data["lux"][i] * 0.34, "b": data["lux"][i] * 0.28,
            "pitch": 0.0, "roll": 0.0, "yaw": 180.0, "compass": 180.0,
            "ax": 0.0, "ay": 0.0, "az": 1.0, "gx": 0.0, "gy": 0.0, "gz": 0.0,
        })
        if i % 500 == 0:
            print(f"  {i}/{n} rows", end="\r", flush=True)

    print(f"\nwrote {n} rows spanning {args.days:.1f} days in {time.time() - t0:.1f}s")
    print(f"database: {cfg.storage.db_path}")
    print("next: python scripts/evaluate.py   (or just start the server)")


if __name__ == "__main__":
    main()
