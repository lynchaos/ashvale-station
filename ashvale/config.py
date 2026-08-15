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

"""Configuration for the Ashvale station.

Everything tunable lives here. Override any field with a YAML file
(default `config.yaml` next to the repo root) or with environment
variables prefixed `ASHVALE_` (e.g. `ASHVALE_SITE__ALTITUDE_M=42`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict

try:
    import yaml  # optional
except Exception:  # pragma: no cover
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class SiteConfig:
    name: str = "ashvale-labs-weather-station"
    latitude: float = 52.2053      # Cambridge, UK
    longitude: float = 0.1218
    altitude_m: float = 15.0       # for sea-level pressure reduction
    timezone: str = "Europe/London"
    indoors: bool = True
    # Where the sensor actually lives, and what has changed around it.
    #
    # This matters more than it looks. Indoors, temperature and humidity are
    # governed by the building, not the sky: the diurnal swing is damped and
    # lagged, and the solar features the model is given correlate weakly with
    # what the thermometer does. Pressure is the exception, which is why the
    # precipitation model runs on tendency rather than indoor humidity.
    #
    # "enclosure" is the part worth changing at runtime. Closing a door or
    # opening a window is a step change in how strongly the sensor is coupled to
    # outside, and the learners carry roughly 55 hours of memory, so they will
    # keep predicting the old regime for two days unless told. POST
    # /api/environment marks the moment and asks for a retrain.
    environment: str = "indoor"        # indoor | sheltered | outdoor
    enclosure: str = "closed"          # closed | ventilated | open           # honest flag, changes how forecasts are worded


@dataclass
class SensorConfig:
    sample_period_s: float = 2.0        # how often we read the HAT
    persist_period_s: float = 30.0      # how often a row hits the database
    rotation_deg: int = 90
    low_light: bool = True
    tcs3400_addr: int = 0x39
    # CPU self-heating compensation: T_true = T_sensor - k * (T_cpu - T_sensor)
    cpu_heat_k: float = 0.55
    cpu_heat_k_min: float = 0.15
    cpu_heat_k_max: float = 1.20
    # Additive RH bias of the element. The datasheet claims about +/-3.5%, but
    # measured against a reference hygrometer this board read 75.4% where the
    # truth was 50.4%, so the clamp has to allow far more than spec. Kept finite
    # so one mistyped reference still cannot run away.
    # Move RH from the element's temperature onto the compensated air temperature
    # via conserved vapour pressure. Physically correct IF the humidity element
    # really sits at temp_raw. Measured on this board it does not: against a
    # reference hygrometer reading 50.4%, the HTS221 reported 75.4%, so it reads
    # HIGH and this correction would push it higher still. The error is an
    # additive element bias, not a thermal gradient. Leave off unless your own
    # reference says otherwise.
    # Optional DS18B20 on the 1-Wire bus, outside the window. When present its
    # reading is logged as outdoor_c and surfaced in the API. It does not feed
    # the forecasting features yet: that needs history to train against.
    outdoor_probe: bool = True
    outdoor_probe_period_s: float = 20.0
    hum_psychrometric: bool = False
    hum_offset: float = 0.0
    hum_offset_min: float = -35.0
    hum_offset_max: float = 35.0
    # Kalman process/measurement noise (per-signal)
    kalman_q_temp: float = 2.0e-6
    kalman_r_temp: float = 0.02
    kalman_q_press: float = 1.0e-5
    kalman_r_press: float = 0.05
    kalman_q_hum: float = 5.0e-5
    kalman_r_hum: float = 0.60


@dataclass
class ModelConfig:
    grid_s: int = 300                                  # 5-minute feature grid
    horizons_s: tuple = (900, 3600, 10800, 21600, 43200, 86400)
    targets: tuple = ("temperature", "humidity", "pressure")
    rls_forgetting: float = 0.9985                     # lambda, ~ 11h memory at 5 min
    rls_delta: float = 100.0                           # P0 = delta * I
    conformal_window: int = 400                        # residuals kept per head
    conformal_alpha: float = 0.10                      # 90% intervals
    conformal_gamma: float = 0.01                      # adaptive conformal step
    train_period_s: float = 600.0                      # retrain cadence
    min_rows_to_train: int = 120
    climatology_min_days_annual: float = 120.0
    anomaly_ewma_lambda: float = 0.15
    anomaly_threshold: float = 12.0                    # Mahalanobis^2 alarm level
    drift_delta: float = 0.05
    drift_lambda: float = 8.0


@dataclass
class StorageConfig:
    db_path: str = str(REPO_ROOT / "data" / "ashvale.db")
    state_dir: str = str(REPO_ROOT / "data" / "state")
    raw_retention_days: float = 7.0
    five_min_retention_days: float = 90.0
    vacuum_period_s: float = 86400.0


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    led_enabled: bool = True
    led_cycle_s: float = 0.4
    # Matrix frame rate. 24 is smooth and costs about 11% of one core on a
    # Zero 2 W. 16 is still fluid and roughly a third cheaper; below about 12
    # the crossfades and sub-pixel motion start to judder, which defeats the
    # point. Set 0 to keep the panel enabled but static-cheap.
    led_fps: float = 24.0


@dataclass
class Config:
    site: SiteConfig = field(default_factory=SiteConfig)
    sensor: SensorConfig = field(default_factory=SensorConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    server: ServerConfig = field(default_factory=ServerConfig)


def _apply(obj: Any, patch: Dict[str, Any]) -> None:
    for key, value in (patch or {}).items():
        if not hasattr(obj, key):
            continue
        current = getattr(obj, key)
        if is_dataclass(current) and isinstance(value, dict):
            _apply(current, value)
        else:
            setattr(obj, key, type(current)(value) if current is not None else value)


def _apply_env(obj: Any, prefix: str = "ASHVALE_") -> None:
    for f in fields(obj):
        current = getattr(obj, f.name)
        if is_dataclass(current):
            _apply_env(current, f"{prefix}{f.name.upper()}__")
            continue
        env_key = f"{prefix}{f.name.upper()}"
        if env_key in os.environ:
            raw = os.environ[env_key]
            try:
                setattr(obj, f.name, type(current)(raw))
            except Exception:
                setattr(obj, f.name, raw)


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg = Config()
    candidate = Path(path) if path else REPO_ROOT / "config.yaml"
    if candidate.exists() and yaml is not None:
        with open(candidate, "r", encoding="utf-8") as fh:
            _apply(cfg, yaml.safe_load(fh) or {})
    _apply_env(cfg)
    Path(cfg.storage.db_path).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.storage.state_dir).mkdir(parents=True, exist_ok=True)
    return cfg


CONFIG = load_config()
