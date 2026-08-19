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

"""HTTP surface. Thin by design: every endpoint is a view over station state.

Backwards compatibility matters here, so `/api/telemetry` returns a
superset of the original payload. Anything already pointed at this Pi
keeps working, and the new fields are simply there when you want them.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import CONFIG, load_overrides, save_overrides
from .dashboard import DASHBOARD_HTML
from .features import FEATURE_NAMES
from .led import LedDisplay
from .methods import describe
from .station import Station

station: Optional[Station] = None
display: Optional[LedDisplay] = None


# Set when the app is shutting down. The SSE generator watches it: without
# that, an open dashboard is an in-flight request that never completes, so
# uvicorn's graceful shutdown blocks until systemd's timeout SIGKILLs the
# process. Reproduced: with no stream client the service stops in 2 s, with
# one open client it was still running after 15 s.
_shutdown = asyncio.Event()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global station, display
    station = Station(CONFIG)
    station.sample_once()
    station.start()
    if CONFIG.server.led_enabled:
        display = LedDisplay(station, CONFIG.server.led_cycle_s, CONFIG.server.led_fps)
        station.display = display          # lets the joystick drive the panel
        display.start()
    try:
        yield
    finally:
        _shutdown.set()
        if display is not None:
            await display.stop()
        if station is not None:
            await station.stop()


app = FastAPI(
    title="Ashvale Station",
    version="1.0.0",
    description="Sense HAT v2 telemetry with online forecasting, calibrated "
                "uncertainty, drift detection and verification.",
    lifespan=lifespan,
)

# Vendored browser libraries. The dashboard used to pull Tailwind, Chart.js,
# hammer, the zoom plugin, KaTeX and two Google fonts from CDNs at runtime,
# which meant the Pi needed internet to render its own UI. Serving them from
# disk costs about 1.4 MB and removes that dependency entirely.
_STATIC = Path(__file__).resolve().parent / "static"
if _STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


def _st() -> Station:
    if station is None:
        raise HTTPException(503, "station not started")
    return station


def _clean(obj: Any) -> Any:
    """JSON is not a superset of IEEE 754. NaN in a response body will
    silently break a browser's JSON.parse, which is a miserable bug to
    chase from a dashboard that just shows dashes."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return None if (f != f or f in (float("inf"), float("-inf"))) else round(f, 6)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, np.ndarray):
        return _clean(obj.tolist())
    return obj


# --------------------------------------------------------------- models

class LabelIn(BaseModel):
    kind: str = Field("rain", description="rain | fog | frost | window_open")
    value: float = Field(..., ge=0.0, le=1.0)
    ts: Optional[float] = None
    note: str = ""


class CalibrationIn(BaseModel):
    reference_c: Optional[float] = Field(None, description="Trusted air temperature in C")
    reset: bool = Field(False, description="Discard the learned coefficient and its "
                                           "covariance, returning to the configured prior")


class SettingsIn(BaseModel):
    """Every field optional: the UI sends only what changed."""
    environment: Optional[str] = None
    enclosure: Optional[str] = None
    note: str = ""
    altitude_m: Optional[float] = Field(None, ge=-430, le=9000)
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    heating: Optional[bool] = None
    heating_setpoint_c: Optional[float] = Field(None, ge=5, le=35)
    thermal_time_constant_h: Optional[float] = Field(None, ge=0.1, le=24)
    hum_psychrometric: Optional[bool] = None
    led_enabled: Optional[bool] = None
    led_fps: Optional[float] = Field(None, ge=4, le=30)


class EnvironmentIn(BaseModel):
    environment: Optional[str] = Field(None, description="indoor | sheltered | outdoor")
    enclosure: Optional[str] = Field(None, description="closed | ventilated | open")
    note: str = Field("", description="what changed, for the log")


class HumidityCalibrationIn(BaseModel):
    reference_pct: Optional[float] = Field(None, ge=0, le=100,
                                           description="Trusted relative humidity in %")
    reset: bool = Field(False, description="Discard the learned offset, returning to "
                                           "the configured prior")


# ------------------------------------------------------------ endpoints

@app.get("/api/telemetry")
def telemetry() -> Dict:
    st = _st()
    live = st.live or st.sample_once()
    colour = live.get("colour") or {}
    return _clean({
        # original contract, preserved
        "timestamp": live.get("timestamp"),
        "temperature": live.get("temp_smooth"),
        "humidity": live.get("hum_smooth"),
        "pressure": live.get("press_slp"),
        "compass": live.get("compass"),
        "pitch": live.get("pitch"),
        "roll": live.get("roll"),
        "yaw": live.get("yaw"),
        "accel": {"x": live.get("ax"), "y": live.get("ay"), "z": live.get("az")},
        "gyro": {"x": live.get("gx"), "y": live.get("gy"), "z": live.get("gz")},
        "color": {"clear": colour.get("clear", live.get("lux", 0)),
                  "red": colour.get("red", live.get("r", 0)),
                  "green": colour.get("green", live.get("g", 0)),
                  "blue": colour.get("blue", live.get("b", 0)),
                  "hex": colour.get("hex", "#334155"),
                  "cct": colour.get("cct")},
        # everything the ML layer adds
        "temperature_raw": live.get("temp_raw"),
        "temperature_compensated": live.get("temp_c"),
        "pressure_station": live.get("press_smooth"),
        "cpu_temp": live.get("cpu_temp"),
        "cpu_offset": live.get("cpu_offset"),
        "compensator_k": live.get("compensator_k"),
        "hum_offset": live.get("hum_offset"),
        "outdoor_c": live.get("outdoor_c"),
        "hum_psychrometric": live.get("hum_psychrometric"),
        "rates": {
            "temperature_c_per_h": live.get("temp_rate"),
            "humidity_pct_per_h": live.get("hum_rate"),
            "pressure_hpa_per_h": live.get("press_rate"),
        },
        "derived": {
            "dew_point": live.get("dew_c"),
            "dew_depression": live.get("dew_depression"),
            "wet_bulb": live.get("wet_bulb"),
            "vpd_hpa": live.get("vpd"),
            "absolute_humidity_g_m3": live.get("abs_humidity"),
            "heat_index": live.get("heat_index"),
            "cloud_index": live.get("cloud_index"),
            "solar_elevation": live.get("solar_elevation"),
            "solar_azimuth": live.get("solar_azimuth"),
            "clear_sky_wm2": live.get("clear_sky_wm2"),
        },
        "health": live.get("health"),
        "novelty_d2": live.get("novelty_d2"),
        "simulated": live.get("simulated"),
    })


@app.get("/api/history")
def history(hours: float = Query(6.0, gt=0, le=24 * 90),
            max_points: int = Query(720, ge=10, le=5000)) -> Dict:
    st = _st()
    cols = ["ts", "temp_smooth", "hum_smooth", "press_slp", "dew_c",
            "temp_rate", "press_rate", "lux"]
    w = st.store.window(hours, cols)
    n = w["ts"].size
    if n == 0:
        return {"n": 0, "series": {}}
    stride = max(1, n // max_points)
    out = {c: w[c][::stride] for c in cols}
    return _clean({
        "n": int(out["ts"].size),
        "hours": hours,
        "series": {
            "ts": out["ts"].tolist(),
            "temperature": out["temp_smooth"].tolist(),
            "humidity": out["hum_smooth"].tolist(),
            "pressure": out["press_slp"].tolist(),
            "dew_point": out["dew_c"].tolist(),
            "temperature_rate": out["temp_rate"].tolist(),
            "pressure_rate": out["press_rate"].tolist(),
            "lux": out["lux"].tolist(),
        },
    })


@app.get("/api/history/range")
def history_range(start: Optional[float] = None, end: Optional[float] = None,
                  hours: Optional[float] = None,
                  bucket: Optional[int] = Query(None, ge=30, le=604800)) -> Dict:
    """Bucket-aggregated telemetry for an arbitrary window.

    Accepts either an explicit epoch `start`/`end` pair or a trailing
    `hours` span. The bucket is chosen automatically from the span unless
    you pin it, so a request for a year does not try to serialise a year
    of five-minute rows to a browser.
    """
    st = _st()
    now = time.time()
    if hours is not None:
        start, end = now - hours * 3600.0, now
    if start is None or end is None:
        raise HTTPException(422, "provide start and end, or hours")
    if end - start > 366 * 86400:
        raise HTTPException(422, "range limited to one year")
    data = st.store.range_series(start, end, bucket)
    return _clean(data)


@app.get("/api/history/daily")
def history_daily(days: int = Query(30, ge=1, le=400)) -> Dict:
    st = _st()
    end = time.time()
    start = end - days * 86400.0
    return _clean({"days": st.store.daily_summary(start, end)})


@app.get("/api/records")
def records() -> Dict:
    """All-time extremes held by this station, each with its timestamp."""
    return _clean(_st().store.extremes())


@app.get("/api/storage")
def storage_stats() -> Dict:
    """Rows per resolution tier plus database size, so retention is visible."""
    st = _st()
    return _clean({
        **st.store.storage_stats(),
        "policy": {
            "raw_retention_days": CONFIG.storage.raw_retention_days,
            "five_min_retention_days": CONFIG.storage.five_min_retention_days,
            "note": "Nothing is deleted, only downsampled. Rows older than the raw "
                    "window fold into 5-minute means, then into hourly means. A "
                    "year of history lands around 30 MB.",
        },
    })


@app.get("/api/export.csv")
def export_csv(start: Optional[float] = None, end: Optional[float] = None,
               hours: Optional[float] = None):
    st = _st()
    now = time.time()
    if hours is not None:
        start, end = now - hours * 3600.0, now
    if start is None or end is None:
        raise HTTPException(422, "provide start and end, or hours")
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(start))
    return StreamingResponse(
        st.store.iter_csv(start, end),
        media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="ashvale-{stamp}.csv"'},
    )


@app.get("/api/methods")
def methods_doc() -> Dict:
    """The Methods tab is generated from this, so it cannot drift from the code."""
    return _clean(describe(CONFIG))


@app.get("/api/forecast")
def forecast(target: Optional[str] = None, refresh: bool = False) -> Dict:
    st = _st()
    if refresh or not st.forecast_bundle:
        st.refresh_forecasts()
    # A cold station has no forecast yet. Return the empty shape rather than
    # a bare {}, so a client never has to distinguish "no data" from "no key".
    bundle = dict(st.forecast_bundle) or {
        "issued_ts": None, "anchors": {},
        "targets": {t: [] for t in CONFIG.model.targets},
        "warming_up": True,
    }
    if target:
        if target not in bundle.get("targets", {}):
            raise HTTPException(404, f"unknown target '{target}'")
        bundle["targets"] = {target: bundle["targets"][target]}
    return _clean(bundle)


@app.get("/api/outlook")
def outlook() -> Dict:
    """Days 2 to 7. Climatology plus a decaying anomaly, honestly labelled."""
    st = _st()
    if not st.outlook_bundle:
        st.refresh_forecasts()
    base = st.outlook_bundle or {
        "issued_ts": None, "ready": False, "annual_terms": False,
        "history_days": round(st.store.span_days(), 2),
        "targets": {t: [] for t in CONFIG.model.targets},
    }
    return _clean({
        **base,
        "method": "harmonic climatology with exponentially decaying anomaly",
        "caveat": "A single point sensor cannot observe approaching systems. "
                  "Treat days 2 to 7 as a climatological outlook, not a forecast.",
    })


@app.get("/api/precipitation")
def precipitation() -> Dict:
    st = _st()
    return _clean(st.precip_bundle or {})


@app.get("/api/anomaly")
def anomaly() -> Dict:
    st = _st()
    return _clean({
        **(st.anomaly_bundle or {}),
        "events": st.monitor.recent(20),
    })


@app.get("/api/models")
def models() -> Dict:
    st = _st()
    return _clean({
        "nowcast": st.nowcast.diagnostics(),
        "climatology": {
            "ready": st.climatology.ready,
            "annual_terms": st.climatology.use_annual,
            "history_days": round(st.climatology.n_days, 2),
            "residual_std": st.climatology.resid_std,
        },
        "precipitation": {
            "coefficients": st.precip.coefficients(),
            "strong_labels": st.precip.n_strong,
            "weak_labels": st.precip.n_weak,
            "logloss_ewma": st.precip.ewma_logloss,
        },
        "calibration": st.tracker.compensator.to_dict(),
    })


def _innovation_histogram(st, bins: int = 21) -> Dict:
    """Distribution of recent standardised Kalman innovations, per signal.

    y/sqrt(S) should be standard normal when a filter is consistent. The single
    NIS number says whether the spread is right on average; this says whether
    the *shape* is right. Skew means systematic bias, excess kurtosis means the
    filter is surprised more often than it admits.
    """
    out = {}
    for name, buf in st.tracker.innovations.items():
        z = np.array(buf, dtype=float)
        z = z[np.isfinite(z)]
        if z.size < 20:
            out[name] = {"counts": [], "n": int(z.size)}
            continue
        clipped = np.clip(z, -4.0, 4.0)
        counts, edges = np.histogram(clipped, bins=bins, range=(-4.0, 4.0))
        out[name] = {
            "counts": [int(c) for c in counts],
            "edges": [round(float(e), 2) for e in edges],
            "n": int(z.size),
            "mean": round(float(np.mean(z)), 4),
            "std": round(float(np.std(z)), 4),
            "skew": round(float(np.mean(((z - z.mean()) / (z.std() or 1.0)) ** 3)), 3),
            "kurtosis": round(float(np.mean(((z - z.mean()) / (z.std() or 1.0)) ** 4)), 3),
        }
    return out


def _reliability_curve(st) -> Dict:
    """Realised coverage against nominal, per horizon.

    The scorecard reports one coverage number per head. This asks the sharper
    question: is the *shape* right. Points below the diagonal mean the intervals
    are lying, and by how much.
    """
    out = []
    for (target, h), head in sorted(st.nowcast.heads.items()):
        cov = head.conformal.empirical_coverage
        if not np.isfinite(cov):
            continue
        out.append({"target": target, "horizon_s": h,
                    "nominal": round(1.0 - head.conformal.alpha_target, 4),
                    "realised": round(float(cov), 4),
                    "n": int(head.n_scored)})
    return {"points": out}


@app.get("/api/nerd")
def nerd() -> Dict:
    """Every internal number the estimator and the learners are carrying.

    Deliberately read-only and computed from live objects rather than stored, so
    it cannot drift from what the station is actually using. Everything here is
    cheap: no matrix inversions, no queries beyond what the caller already pays
    for. `theta` is returned per head so the UI can show which of the 33 features
    each horizon actually leans on, which is the single most revealing view of
    what the model has learned.
    """
    st = _st()
    tr = st.tracker

    filters = {}
    for name, kf in tr.filters.items():
        P = np.asarray(kf.P, dtype=float)
        filters[name] = {
            "level": float(kf.x[0]), "rate_per_h": float(kf.x[1]) * 3600.0,
            "nis": float(kf.nis),
            "p_level": float(P[0, 0]), "p_rate": float(P[1, 1]),
            "p_cross": float(P[0, 1]),
            "sigma_level": float(np.sqrt(max(P[0, 0], 0.0))),
            "q": float(kf.q), "r": float(kf.r),
            "initialised": bool(kf.initialised),
        }

    heads = []
    for (target, h), head in sorted(st.nowcast.heads.items()):
        m = head.model
        P = np.asarray(m.P, dtype=float)
        theta = np.asarray(m.theta, dtype=float)
        # Condition number of P says whether the 33 directions are being excited
        # evenly. A huge value means some directions carry almost no information
        # and the fit there is effectively arbitrary, which is the quiet failure
        # the trace cap only partly protects against. eigvalsh because P is
        # symmetric by construction.
        try:
            ev = np.linalg.eigvalsh(P)
            lo, hi = float(np.min(ev)), float(np.max(ev))
            cond = float(hi / lo) if lo > 1e-12 else float("inf")
        except np.linalg.LinAlgError:
            cond = float("nan")
        heads.append({
            "target": target, "horizon_s": h,
            "n_updates": int(m.n_updates),
            "trace_p": float(np.trace(P)),
            "cond_p": cond,
            "theta_norm": float(np.linalg.norm(theta)),
            "rmse_ewma": float(np.sqrt(max(m.ewma_sq_error, 0.0))),
            "lam": float(m.lam), "p_max": float(m.p_max),
            "eff_memory": float(1.0 / max(1.0 - m.lam, 1e-9)),
            "alpha": float(head.conformal.alpha),
            "alpha_target": float(head.conformal.alpha_target),
            "coverage": (float(head.conformal.empirical_coverage)
                         if np.isfinite(head.conformal.empirical_coverage) else None),
            "halfwidth": (float(head.conformal.quantile())
                          if np.isfinite(head.conformal.quantile()) else None),
            "weights": {k: float(v) for k, v in
                        zip(("persistence", "climatology", "learned"), head.weights)},
            "theta": [round(float(v), 6) for v in theta],
        })

    mono = st.monitor
    nov = getattr(mono, "novelty", None)
    ph = getattr(mono, "drift", None)
    monitoring = {
        "novelty": {
            "d2": float(getattr(nov, "last_d2", 0.0)) if nov is not None else None,
            "threshold": float(getattr(nov, "threshold", 0.0)) if nov is not None else None,
            "n": int(getattr(nov, "n", 0)) if nov is not None else None,
            "dims": int(getattr(nov, "d", 0)) if nov is not None else None,
            "z": [round(float(v), 4) for v in np.asarray(getattr(nov, "z", []), dtype=float)]
            if nov is not None else [],
        },
        "drift": {
            "m_pos": float(getattr(ph, "m_pos", 0.0)) if ph is not None else None,
            "m_neg": float(getattr(ph, "m_neg", 0.0)) if ph is not None else None,
            "mean": float(getattr(ph, "mean", 0.0)) if ph is not None else None,
            "n": int(getattr(ph, "n", 0)) if ph is not None else None,
            "alarms": int(getattr(ph, "n_alarms", 0)) if ph is not None else None,
            "delta": float(getattr(ph, "delta", 0.0)) if ph is not None else None,
        },
    }

    return _clean({
        "feature_names": list(FEATURE_NAMES),
        "filters": filters,
        "compensators": {
            "thermal": tr.compensator.to_dict(),
            "humidity": tr.hum_compensator.to_dict(),
        },
        "heads": heads,
        "climatology": {
            "ready": st.climatology.ready,
            "annual_terms": st.climatology.use_annual,
            "history_days": round(st.climatology.n_days, 3),
            "diurnal_harmonics": st.climatology.kd,
            "annual_harmonics": st.climatology.ka,
            "ridge": st.climatology.ridge,
            "residual_std": st.climatology.resid_std,
            "n_coefficients": {k: len(v) for k, v in st.climatology.coef.items()},
        },
        "precipitation": {
            "coefficients": st.precip.coefficients(),
            "strong_labels": st.precip.n_strong,
            "weak_labels": st.precip.n_weak,
            "logloss_ewma": st.precip.ewma_logloss,
        },
        "monitoring": monitoring,
        "innovation": _innovation_histogram(st),
        "reliability": _reliability_curve(st),
    })


@app.get("/api/scorecard")
def scorecard() -> Dict:
    st = _st()
    rows = st.store.scorecard()
    return _clean({
        "rows": rows,
        "explainer": "skill = 1 - MAE/MAE_persistence. Above zero means the "
                     "model beats 'nothing changes'. Below zero means it does not, "
                     "and persistence should be shipped instead.",
    })


@app.post("/api/verify")
def verify_now() -> Dict:
    return _clean(_st().verify())


@app.post("/api/train")
def train_now(hours: float = Query(24 * 30, gt=1)) -> Dict:
    return _clean(_st().train(hours))


@app.post("/api/label")
def add_label(body: LabelIn) -> Dict:
    return _clean(_st().add_label(body.kind, body.value, body.ts, body.note))


@app.post("/api/calibrate")
def calibrate(body: CalibrationIn) -> Dict:
    st = _st()
    if body.reset:
        return _clean(st.reset_calibration())
    if body.reference_c is None:
        raise HTTPException(422, "provide reference_c, or reset=true")
    result = st.calibrate_temperature(body.reference_c)
    if "error" in result:
        raise HTTPException(409, result["error"])
    return _clean(result)


@app.post("/api/calibrate/humidity")
def calibrate_humidity(body: HumidityCalibrationIn) -> Dict:
    st = _st()
    if body.reset:
        return _clean(st.reset_humidity_calibration())
    if body.reference_pct is None:
        raise HTTPException(422, "provide reference_pct, or reset=true")
    result = st.calibrate_humidity(body.reference_pct)
    if "error" in result:
        raise HTTPException(409, result["error"])
    return _clean(result)


@app.post("/api/recompute")
def recompute() -> Dict:
    """Re-derive every compensated column in the history from the raw values.

    Run after a calibration to remove the step it leaves behind. Safe to repeat:
    it always starts from the untouched raw columns, never from a previous
    result, so it cannot compound.
    """
    result = _st().recompute_history()
    return _clean(result)


@app.post("/api/environment")
def environment(body: EnvironmentIn) -> Dict:
    """Tell the station its surroundings changed, and have it react.

    Marks a discontinuity and queues a retrain, because the learners' 55 hour
    memory would otherwise keep predicting the old regime for two days.
    """
    valid_env = {"indoor", "sheltered", "outdoor"}
    valid_enc = {"closed", "ventilated", "open"}
    if body.environment and body.environment not in valid_env:
        raise HTTPException(422, f"environment must be one of {sorted(valid_env)}")
    if body.enclosure and body.enclosure not in valid_enc:
        raise HTTPException(422, f"enclosure must be one of {sorted(valid_enc)}")
    if not body.environment and not body.enclosure:
        raise HTTPException(422, "provide environment, enclosure, or both")
    return _clean(_st().set_environment(body.environment, body.enclosure, body.note))


@app.get("/api/settings")
def get_settings() -> Dict:
    st = _st()
    return _clean({
        "site": {"environment": CONFIG.site.environment,
                 "enclosure": CONFIG.site.enclosure,
                 "altitude_m": CONFIG.site.altitude_m,
                 "latitude": CONFIG.site.latitude,
                 "longitude": CONFIG.site.longitude,
                 "timezone": CONFIG.site.timezone,
                 "heating": CONFIG.site.heating,
                 "heating_setpoint_c": CONFIG.site.heating_setpoint_c,
                 "thermal_time_constant_h": CONFIG.site.thermal_time_constant_h,
                 "name": CONFIG.site.name},
        "sensor": {"hum_psychrometric": CONFIG.sensor.hum_psychrometric,
                   "cpu_heat_k": round(st.tracker.compensator.k, 4),
                   "hum_offset": round(st.tracker.hum_compensator.offset, 3)},
        "server": {"led_enabled": CONFIG.server.led_enabled,
                   "led_fps": CONFIG.server.led_fps},
        "options": {
            "environment": ["indoor", "sheltered", "outdoor"],
            "enclosure": ["closed", "ventilated", "open"],
        },
        "overrides": load_overrides(CONFIG),
    })


@app.post("/api/settings")
def post_settings(body: SettingsIn) -> Dict:
    """Apply settings live and persist them to the overlay.

    Everything here takes effect without a restart, because a settings page that
    needs one is a settings page people stop trusting. Site geometry is read per
    sample, the compensator flag is a field on a live object, and the display
    reads its own rate each frame.
    """
    st = _st()
    patch: Dict[str, Dict] = {}
    applied, needs_recompute = [], False

    if body.environment or body.enclosure:
        r = st.set_environment(body.environment, body.enclosure, body.note)
        if r.get("changed"):
            applied.append(r["detail"])
            patch.setdefault("site", {}).update(
                {"environment": CONFIG.site.environment,
                 "enclosure": CONFIG.site.enclosure})

    for name, value in (("altitude_m", body.altitude_m),
                        ("latitude", body.latitude),
                        ("longitude", body.longitude)):
        if value is not None and value != getattr(CONFIG.site, name):
            applied.append(f"{name} {getattr(CONFIG.site, name)} -> {value}")
            setattr(CONFIG.site, name, float(value))
            patch.setdefault("site", {})[name] = float(value)
            # Altitude feeds the sea-level reduction on every stored row, so the
            # history is now inconsistent with the new value until re-derived.
            needs_recompute = needs_recompute or name == "altitude_m"

    # Turning the thermostat model on or off changes which process the heads are
    # fitting, so it is a regime change and gets the same treatment as a door.
    if body.heating is not None and body.heating != CONFIG.site.heating:
        CONFIG.site.heating = bool(body.heating)
        patch.setdefault("site", {})["heating"] = bool(body.heating)
        applied.append(f"heating {'on' if body.heating else 'off'}")
        st.store.log_event("discontinuity", "warn",
                           f"heating {'on' if body.heating else 'off'}")
        st.monitor.retrain_requested = True

    for name, value, label in (
            ("heating_setpoint_c", body.heating_setpoint_c, "setpoint"),
            ("thermal_time_constant_h", body.thermal_time_constant_h, "time constant")):
        if value is not None and value != getattr(CONFIG.site, name):
            applied.append(f"{label} {getattr(CONFIG.site, name)} -> {value}")
            setattr(CONFIG.site, name, float(value))
            patch.setdefault("site", {})[name] = float(value)

    if body.hum_psychrometric is not None and \
            body.hum_psychrometric != CONFIG.sensor.hum_psychrometric:
        CONFIG.sensor.hum_psychrometric = bool(body.hum_psychrometric)
        st.tracker.hum_compensator.psychrometric = bool(body.hum_psychrometric)
        patch.setdefault("sensor", {})["hum_psychrometric"] = bool(body.hum_psychrometric)
        applied.append(f"psychrometric correction {'on' if body.hum_psychrometric else 'off'}")
        needs_recompute = True

    if body.led_enabled is not None and body.led_enabled != CONFIG.server.led_enabled:
        CONFIG.server.led_enabled = bool(body.led_enabled)
        patch.setdefault("server", {})["led_enabled"] = bool(body.led_enabled)
        if display is not None:
            display.enabled = bool(body.led_enabled)
            if not body.led_enabled:
                st.board.clear()
        applied.append(f"matrix {'on' if body.led_enabled else 'off'}")

    if body.led_fps is not None and body.led_fps != CONFIG.server.led_fps:
        CONFIG.server.led_fps = float(body.led_fps)
        patch.setdefault("server", {})["led_fps"] = float(body.led_fps)
        if display is not None:
            display.fps = float(body.led_fps)
        applied.append(f"matrix {body.led_fps:g} fps")

    if patch:
        save_overrides(CONFIG, patch)
        st.store.log_event("settings", "info", "; ".join(applied))

    return _clean({"applied": applied, "changed": bool(applied),
                   "needs_recompute": needs_recompute})


@app.get("/api/status")
def status() -> Dict:
    st = _st()
    return _clean({
        **st.status(),
        "display_frame": display.frame_name if display else None,
        "outdoor_probe": (st.probe.status() if st.probe is not None else None),
        "environment": CONFIG.site.environment,
        "enclosure": CONFIG.site.enclosure,
        "events": st.store.recent_events(15),
    })


@app.get("/api/events")
def events(limit: int = Query(50, ge=1, le=500)) -> List[Dict]:
    return _clean(_st().store.recent_events(limit))


@app.get("/api/stream")
async def stream(request: Request):
    """Server-sent events. One connection instead of a poll every 2 seconds,
    which on a Zero 2 W is the difference between 4% and 0.4% CPU.

    The loop exits on shutdown or client disconnect. Both matter: an endless
    generator keeps the response in flight, and uvicorn will not finish a
    graceful shutdown while one is open.
    """
    async def gen():
        while not _shutdown.is_set():
            if await request.is_disconnected():
                break
            st = _st()
            payload = {
                "telemetry": telemetry(),
                "precipitation": _clean(st.precip_bundle or {}),
                "health": st.monitor.health.overall,
                "drift_stress": round(st.monitor.drift.stress, 3),
            }
            yield f"data: {json.dumps(payload)}\n\n"
            # Wait on the shutdown event rather than sleeping blindly, so a stop
            # is honoured immediately instead of up to 2 s later.
            try:
                await asyncio.wait_for(_shutdown.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/", response_class=HTMLResponse)
def dashboard() -> Response:
    # The page is generated from live config and changes with every deploy, and
    # it carried no cache headers, so a browser could hold an old copy
    # indefinitely and show layout bugs that were already fixed. The vendored
    # assets under /static are fingerprint-free too, but they only change when
    # the station is updated, so revalidation is enough for them.
    return HTMLResponse(DASHBOARD_HTML,
                        headers={"Cache-Control": "no-cache, must-revalidate"})
