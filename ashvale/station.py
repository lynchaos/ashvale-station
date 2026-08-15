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

"""The station: everything wired together and running on its own clocks.

Four asynchronous loops, deliberately decoupled so a slow one cannot
starve a fast one:

  sample   (2 s)    read hardware, run the Kalman bank, keep live state
  persist  (30 s)   one row to SQLite
  train    (10 min) rebuild the feature grid, update every head, re-fit
                    climatology, emit a fresh forecast bundle
  verify   (5 min)  score forecasts whose validity time has arrived, feed
                    the errors to conformal calibration and drift
                    detection, write the scorecard

The verify loop is the one most projects skip and the one that makes the
difference. A forecast that is never scored is an opinion; a forecast
that is scored against persistence is a measurement.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from . import physics
from .config import Config
from .estimation import SignalTracker
from .features import build_features
from .models.anomaly import AnomalyMonitor
from .models.climatology import HarmonicClimatology
from .models.nowcast import NowcastEnsemble
from .models.precip import PrecipitationModel, proxy_wet_label, zambretti
from .sensors import SenseBoard, enrich
from .storage import Store, resample

STATE_VERSION = 1


class Station:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.storage.db_path)
        self.board = SenseBoard(
            rotation=cfg.sensor.rotation_deg,
            low_light=cfg.sensor.low_light,
            tcs_addr=cfg.sensor.tcs3400_addr,
            latitude=cfg.site.latitude,
            longitude=cfg.site.longitude,
        )
        self.tracker = SignalTracker(cfg)
        self.nowcast = NowcastEnsemble(cfg.model.targets, cfg.model.horizons_s, cfg.model)
        self.climatology = HarmonicClimatology(
            cfg.model.targets, min_days_annual=cfg.model.climatology_min_days_annual
        )
        self.precip = PrecipitationModel()
        self.monitor = AnomalyMonitor(cfg.model)

        self.live: Dict[str, Any] = {}
        self.forecast_bundle: Dict[str, Any] = {}
        self.outlook_bundle: Dict[str, Any] = {}
        self.precip_bundle: Dict[str, Any] = {}
        self.anomaly_bundle: Dict[str, Any] = {}
        self.last_train: float = 0.0
        self.last_persist: float = 0.0
        self.last_compact: float = 0.0
        self.training_log: List[Dict] = []
        self._tasks: List[asyncio.Task] = []
        self._stop = asyncio.Event()

        self.state_path = Path(cfg.storage.state_dir) / "station_state.json"
        self.load_state()

    # ------------------------------------------------------------ state

    def save_state(self) -> None:
        payload = {
            "version": STATE_VERSION,
            "saved_at": time.time(),
            "tracker": self.tracker.to_dict(),
            "nowcast": self.nowcast.to_dict(),
            "climatology": self.climatology.to_dict(),
            "precip": self.precip.to_dict(),
            "monitor": self.monitor.to_dict(),
        }
        tmp = self.state_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        tmp.replace(self.state_path)     # atomic, survives a power cut mid-write

    def load_state(self) -> bool:
        if not self.state_path.exists():
            return False
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                s = json.load(fh)
            if s.get("version") != STATE_VERSION:
                return False
            self.tracker.load_dict(s["tracker"])
            self.nowcast.load_dict(s["nowcast"])
            self.climatology.load_dict(s["climatology"])
            self.precip.load_dict(s["precip"])
            self.monitor.load_dict(s["monitor"])
            return True
        except Exception as exc:
            self.store.log_event("state", "warn", f"could not restore state: {exc}")
            return False

    # ----------------------------------------------------------- sample

    def sample_once(self) -> Dict[str, Any]:
        ts = time.time()
        raw = self.board.read()
        raw = enrich(raw, self.cfg.site.altitude_m)
        est = self.tracker.step(ts, raw.get("temp_raw", float("nan")),
                                raw.get("hum", float("nan")),
                                raw.get("press", float("nan")),
                                raw.get("cpu_temp", float("nan")))

        temp_c = est["temp_smooth"]
        slp = float(physics.sea_level_pressure(est["press_smooth"], temp_c,
                                               self.cfg.site.altitude_m))
        dew = float(physics.dew_point(temp_c, est["hum_smooth"]))
        elev, azim = physics.solar_position(ts, self.cfg.site.latitude,
                                            self.cfg.site.longitude)
        expected = float(physics.clear_sky_irradiance(elev))
        lux = float(raw.get("lux", 0.0) or 0.0)
        cloud = (float(np.clip(1.0 - lux / max(expected * 45.0, 1.0), 0.0, 1.0))
                 if elev > 5.0 else 0.5)

        row = {
            "ts": ts,
            "temp_raw": raw.get("temp_raw"),
            "temp_c": est["temp_c"],
            "temp_smooth": temp_c,
            "temp_rate": est["temp_rate"],
            "hum": raw.get("hum"),
            "hum_smooth": est["hum_smooth"],
            "press": raw.get("press"),
            "press_slp": slp,
            "press_smooth": est["press_smooth"],
            "press_rate": est["press_rate"],
            "cpu_temp": raw.get("cpu_temp"),
            "dew_c": dew,
            "lux": lux,
            "r": raw.get("r"), "g": raw.get("g"), "b": raw.get("b"),
            "pitch": raw.get("pitch"), "roll": raw.get("roll"),
            "yaw": raw.get("yaw"), "compass": raw.get("compass"),
            "ax": raw.get("ax"), "ay": raw.get("ay"), "az": raw.get("az"),
            "gx": raw.get("gx"), "gy": raw.get("gy"), "gz": raw.get("gz"),
        }

        anomaly = self.monitor.observe(ts, {
            "temp_c": temp_c, "hum": est["hum_smooth"], "press_slp": slp,
            "temp_rate": est["temp_rate"], "press_rate": est["press_rate"],
            "dew_c": dew, "cpu_temp": raw.get("cpu_temp"),
        })
        self.anomaly_bundle = anomaly

        self.live = {
            **row,
            "timestamp": time.strftime("%H:%M:%S", time.localtime(ts)),
            "colour": raw.get("colour", {}),
            "simulated": bool(raw.get("simulated", not self.board.available)),
            "dew_depression": temp_c - dew,
            "vpd": float(physics.vapour_pressure_deficit(temp_c, est["hum_smooth"])),
            "wet_bulb": float(physics.wet_bulb(temp_c, est["hum_smooth"])),
            "heat_index": float(physics.heat_index(temp_c, est["hum_smooth"])),
            "abs_humidity": float(physics.absolute_humidity(temp_c, est["hum_smooth"])),
            "solar_elevation": float(elev),
            "solar_azimuth": float(azim),
            "clear_sky_wm2": expected,
            "cloud_index": cloud,
            "cpu_offset": (raw.get("cpu_temp") or float("nan")) - (raw.get("temp_raw") or float("nan")),
            "compensator_k": self.tracker.compensator.k,
            "health": anomaly["health_overall"],
            "novelty_d2": anomaly["novelty"].get("d2", 0.0),
        }
        self._update_precip()
        return self.live

    def _observation_vector(self) -> Dict[str, float]:
        live = self.live
        hist = self.store.window(8.0, ["ts", "press_slp", "temp_c", "dew_c"])
        tend = {"tend_1h": live.get("press_rate", 0.0),
                "tend_3h": live.get("press_rate", 0.0),
                "tend_6h": live.get("press_rate", 0.0)}
        if hist["ts"].size > 5:
            now = hist["ts"][-1]
            for key, hours in (("tend_1h", 1.0), ("tend_3h", 3.0), ("tend_6h", 6.0)):
                idx = np.searchsorted(hist["ts"], now - hours * 3600.0)
                if 0 <= idx < hist["ts"].size - 1:
                    dtp = (now - hist["ts"][idx]) / 3600.0
                    if dtp > 0.25:
                        tend[key] = float((hist["press_slp"][-1] - hist["press_slp"][idx]) / dtp)
        dew_dep = live.get("dew_depression", 5.0)
        dew_dep_rate = 0.0
        if hist["ts"].size > 5:
            idx = np.searchsorted(hist["ts"], hist["ts"][-1] - 3600.0)
            if 0 <= idx < hist["ts"].size - 1:
                past = hist["temp_c"][idx] - hist["dew_c"][idx]
                dew_dep_rate = float(dew_dep - past)
        return {
            "slp": live.get("press_slp", 1013.25),
            "rh": live.get("hum_smooth", 60.0),
            "dew_depression": dew_dep,
            "dew_dep_rate": dew_dep_rate,
            "cloud_index": live.get("cloud_index", 0.5),
            "temp_dev": self.climatology.anomaly_now(
                "temperature", live.get("ts", time.time()), live.get("temp_smooth", 0.0)
            ),
            "wet_bulb_depression": live.get("temp_smooth", 0.0) - live.get("wet_bulb", 0.0),
            **tend,
        }

    def _update_precip(self) -> None:
        obs = self._observation_vector()
        zam = zambretti(obs["slp"], obs["tend_3h"], self.live.get("ts"),
                        self.cfg.site.latitude)
        self.precip_bundle = self.precip.predict(obs, zam)
        self.precip_bundle["indoors_caveat"] = self.cfg.site.indoors

        y = proxy_wet_label(obs["rh"], obs["dew_depression"], obs["cloud_index"])
        if y is not None and int(self.live.get("ts", 0)) % 300 < self.cfg.sensor.sample_period_s:
            self.precip.learn(obs, zam, y, strong=False)

    def add_label(self, kind: str, value: float, ts: Optional[float] = None,
                  note: str = "") -> Dict:
        """Human-in-the-loop ground truth. Worth ten times a proxy label."""
        ts = ts or time.time()
        self.store.insert_label(ts, kind, value, note)
        if kind == "rain":
            obs = self._observation_vector()
            zam = zambretti(obs["slp"], obs["tend_3h"], ts, self.cfg.site.latitude)
            loss = self.precip.learn(obs, zam, float(value), strong=True)
            self.store.log_event("label", "info",
                                 f"strong rain label {value} accepted, loss {loss:.3f}", ts)
            return {"accepted": True, "loss": loss, "strong_labels": self.precip.n_strong}
        return {"accepted": True}

    def calibrate_temperature(self, reference_c: float) -> Dict:
        raw = self.live.get("temp_raw")
        cpu = self.live.get("cpu_temp")
        if raw is None or cpu is None:
            return {"error": "no live reading yet"}
        result = self.tracker.compensator.calibrate(float(raw), float(cpu), float(reference_c))
        self.store.log_event("calibration", "info",
                             f"k -> {result['k']:.3f} (residual {result['residual']:+.2f} C)")
        return result

    def reset_calibration(self) -> Dict:
        """Return the self-heating coefficient to its configured prior.

        Worth having: a single mistyped reference reading can drive `k`
        to its clamp, and because state persists across restarts it will
        stay there quietly biasing every reading until you notice.
        """
        from .estimation import ThermalCompensator
        self.tracker.compensator = ThermalCompensator(
            self.cfg.sensor.cpu_heat_k, self.cfg.sensor.cpu_heat_k_min,
            self.cfg.sensor.cpu_heat_k_max,
        )
        self.save_state()
        self.store.log_event("calibration", "info",
                             f"coefficient reset to prior k={self.cfg.sensor.cpu_heat_k}")
        return {"k": self.tracker.compensator.k, "reset": True, "n": 0}

    # ------------------------------------------------------------ train

    def build_training_grid(self, hours: float = 24 * 30):
        raw = self.store.window(hours, ["ts", "temp_smooth", "hum_smooth",
                                        "press_slp", "lux"])
        if raw["ts"].size < 10:
            return None
        grid_ts, cols = resample(
            raw["ts"],
            {"temperature": raw["temp_smooth"], "humidity": raw["hum_smooth"],
             "pressure": raw["press_slp"], "lux": raw["lux"]},
            self.cfg.model.grid_s,
        )
        if grid_ts.size < self.cfg.model.min_rows_to_train:
            return None
        X, valid = build_features(
            grid_ts, cols["temperature"], cols["humidity"], cols["pressure"],
            cols["lux"], self.cfg.model.grid_s,
            self.cfg.site.latitude, self.cfg.site.longitude,
        )
        return grid_ts, cols, X, valid

    def train(self, hours: float = 24 * 30) -> Dict:
        t_start = time.time()
        built = self.build_training_grid(hours)
        if built is None:
            return {"trained": False,
                    "reason": f"need at least {self.cfg.model.min_rows_to_train} grid rows"}
        grid_ts, cols, X, valid = built

        clim_scores = self.climatology.fit(grid_ts, cols, valid)
        counts = self.nowcast.fit(X, valid, cols, self.climatology, grid_ts)

        self.last_train = time.time()
        self.monitor.clear_retrain_flag()
        entry = {
            "ts": self.last_train,
            "grid_rows": int(grid_ts.size),
            "valid_rows": int(valid.sum()),
            "span_days": round(float((grid_ts[-1] - grid_ts[0]) / 86400.0), 2),
            "pairs": counts,
            "climatology_resid_std": {k: round(v, 3) for k, v in clim_scores.items()},
            "annual_terms": self.climatology.use_annual,
            "seconds": round(time.time() - t_start, 2),
        }
        self.training_log = ([entry] + self.training_log)[:20]
        self.store.log_event("train", "info",
                             f"retrained on {grid_ts.size} grid rows in {entry['seconds']}s")
        self.refresh_forecasts()
        self.save_state()
        return {"trained": True, **entry}

    # --------------------------------------------------------- forecast

    def refresh_forecasts(self, persist: bool = True) -> Dict:
        built = self.build_training_grid(hours=48.0)
        now = time.time()
        if built is None or not self.live:
            return {}
        grid_ts, cols, X, valid = built
        x_now = X[-1]

        anchors = {
            "temperature": float(self.live.get("temp_smooth", cols["temperature"][-1])),
            "humidity": float(self.live.get("hum_smooth", cols["humidity"][-1])),
            "pressure": float(self.live.get("press_slp", cols["pressure"][-1])),
        }
        fc = self.nowcast.forecast(x_now, anchors, now, self.climatology)

        bundle: Dict[str, Any] = {"issued_ts": now, "anchors": anchors, "targets": {}}
        for target, per_h in fc.items():
            series = []
            for h in sorted(per_h):
                p = per_h[h]
                series.append({
                    "horizon_s": h,
                    "horizon_label": _fmt_horizon(h),
                    "valid_ts": now + h,
                    "mu": round(p["mu"], 3),
                    "lo": round(p["lo"], 3),
                    "hi": round(p["hi"], 3),
                    "delta": round(p["delta"], 3),
                    "weights": {k: round(v, 3) for k, v in p["weights"].items()},
                })
                if persist:
                    self.store.insert_forecast(now, h, target, p["mu"], p["lo"],
                                               p["hi"], "ensemble")
            bundle["targets"][target] = series
        self.forecast_bundle = bundle

        self.outlook_bundle = {
            "issued_ts": now,
            "ready": self.climatology.ready,
            "annual_terms": self.climatology.use_annual,
            "history_days": round(self.store.span_days(), 2),
            "targets": {
                t: self.climatology.outlook(
                    t, now, days=7,
                    anomaly=self.climatology.anomaly_now(t, now, anchors.get(t, 0.0)),
                )
                for t in self.cfg.model.targets
            },
        }
        return bundle

    # ----------------------------------------------------------- verify

    def verify(self) -> Dict:
        """Score matured forecasts against truth and against persistence."""
        due = self.store.due_forecasts()
        if not due:
            return {"scored": 0}

        hist = self.store.window(24 * 8, ["ts", "temp_smooth", "hum_smooth", "press_slp"])
        if hist["ts"].size < 5:
            return {"scored": 0}
        series = {"temperature": hist["temp_smooth"], "humidity": hist["hum_smooth"],
                  "pressure": hist["press_slp"]}

        def value_at(target: str, ts: float) -> Optional[float]:
            idx = int(np.searchsorted(hist["ts"], ts))
            if idx <= 0 or idx >= hist["ts"].size:
                return None
            if abs(hist["ts"][idx] - ts) > 900:
                return None
            return float(series[target][idx])

        buckets: Dict[tuple, Dict[str, List[float]]] = {}
        scored = 0
        for row in due:
            target, h = row["target"], int(row["horizon_s"])
            truth = value_at(target, row["valid_ts"])
            anchor = value_at(target, row["issued_ts"])
            if truth is None or anchor is None:
                continue
            key = (target, h)
            b = buckets.setdefault(key, {"err": [], "pers": [], "cov": []})
            err = truth - row["mu"]
            b["err"].append(err)
            b["pers"].append(truth - anchor)
            b["cov"].append(1.0 if row["lo"] <= truth <= row["hi"] else 0.0)
            head = self.nowcast.heads.get(key)
            if head is not None:
                head.conformal.observe(err, covered=bool(row["lo"] <= truth <= row["hi"]))
            if h <= 10800:
                self.monitor.observe_error(row["valid_ts"], abs(err))
            scored += 1

        now = time.time()
        for (target, h), b in buckets.items():
            e = np.asarray(b["err"], dtype=float)
            p = np.asarray(b["pers"], dtype=float)
            mae = float(np.mean(np.abs(e)))
            mae_p = float(np.mean(np.abs(p)))
            self.store.insert_score(
                now, target, h,
                mae=mae,
                rmse=float(np.sqrt(np.mean(e ** 2))),
                bias=float(np.mean(e)),
                mae_persistence=mae_p,
                skill=float(1.0 - mae / mae_p) if mae_p > 1e-9 else 0.0,
                coverage=float(np.mean(b["cov"])),
                n=int(e.size),
            )

        with self.store._conn() as conn:
            conn.execute("DELETE FROM forecasts WHERE valid_ts <= ?", (now - 3600,))
        return {"scored": scored, "buckets": len(buckets)}

    # ------------------------------------------------------------ loops

    async def _loop_sample(self):
        period = self.cfg.sensor.sample_period_s
        while not self._stop.is_set():
            try:
                self.sample_once()
                now = time.time()
                if now - self.last_persist >= self.cfg.sensor.persist_period_s:
                    self.store.insert_telemetry(self.live)
                    self.last_persist = now
            except Exception as exc:
                self.store.log_event("sample", "error", repr(exc))
            await asyncio.sleep(period)

    async def _loop_train(self):
        await asyncio.sleep(5)
        try:
            self.train()
        except Exception as exc:
            self.store.log_event("train", "error", repr(exc))
        while not self._stop.is_set():
            await asyncio.sleep(30)
            now = time.time()
            due = (now - self.last_train) >= self.cfg.model.train_period_s
            if due or self.monitor.retrain_requested:
                try:
                    await asyncio.to_thread(self.train)
                except Exception as exc:
                    self.store.log_event("train", "error", repr(exc))

    async def _loop_verify(self):
        await asyncio.sleep(60)
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.verify)
            except Exception as exc:
                self.store.log_event("verify", "error", repr(exc))
            await asyncio.sleep(300)

    async def _loop_maintenance(self):
        while not self._stop.is_set():
            await asyncio.sleep(3600)
            now = time.time()
            if now - self.last_compact >= self.cfg.storage.vacuum_period_s:
                try:
                    removed = await asyncio.to_thread(
                        self.store.compact,
                        self.cfg.storage.raw_retention_days,
                        self.cfg.storage.five_min_retention_days,
                    )
                    self.last_compact = now
                    self.store.log_event("compact", "info", json.dumps(removed))
                except Exception as exc:
                    self.store.log_event("compact", "error", repr(exc))
            self.save_state()

    def start(self) -> None:
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._loop_sample()),
            asyncio.create_task(self._loop_train()),
            asyncio.create_task(self._loop_verify()),
            asyncio.create_task(self._loop_maintenance()),
        ]

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        try:
            self.save_state()
        except Exception:
            pass

    # ------------------------------------------------------------ views

    def status(self) -> Dict:
        return {
            "site": self.cfg.site.name,
            "hardware": "sense-hat-v2" if self.board.available else "simulator",
            "colour_sensor": self.board.has_colour,
            "rows": self.store.row_count(),
            "history_days": round(self.store.span_days(), 3),
            "last_train": self.last_train,
            "next_train_in_s": max(0.0, self.cfg.model.train_period_s
                                   - (time.time() - self.last_train)),
            "climatology_ready": self.climatology.ready,
            "annual_terms": self.climatology.use_annual,
            "compensator_k": round(self.tracker.compensator.k, 4),
            "calibrations": self.tracker.compensator.n_calibrations,
            "health": self.monitor.health.overall,
            "drift_stress": round(self.monitor.drift.stress, 3),
            "retrain_requested": self.monitor.retrain_requested,
            "training_log": self.training_log[:5],
        }


def _fmt_horizon(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
