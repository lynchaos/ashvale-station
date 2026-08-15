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

"""Durable storage: SQLite in WAL mode with tiered downsampling.

An SD card is a consumable. The write pattern here is deliberately
gentle: one row every `persist_period_s`, WAL journalling, a compaction
pass that folds week-old raw rows into 5-minute means and quarter-old
5-minute rows into hourly means. A year of station history lands around
30 MB, which the Pi will not notice.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

import numpy as np

TIER_RAW = 0
TIER_5MIN = 1
TIER_HOUR = 2

COLUMNS = [
    "ts", "temp_raw", "temp_c", "temp_smooth", "temp_rate", "hum", "hum_smooth",
    "press", "press_slp", "press_smooth", "press_rate", "cpu_temp", "dew_c",
    "lux", "r", "g", "b", "pitch", "roll", "yaw", "compass",
    "ax", "ay", "az", "gx", "gy", "gz",
]

SCHEMA = f"""
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;

CREATE TABLE IF NOT EXISTS telemetry (
    ts REAL PRIMARY KEY,
    {", ".join(f"{c} REAL" for c in COLUMNS if c != "ts")},
    tier INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_telemetry_tier_ts ON telemetry(tier, ts);

CREATE TABLE IF NOT EXISTS forecasts (
    issued_ts REAL NOT NULL,
    valid_ts REAL NOT NULL,
    horizon_s INTEGER NOT NULL,
    target TEXT NOT NULL,
    mu REAL, lo REAL, hi REAL,
    model TEXT,
    PRIMARY KEY (issued_ts, horizon_s, target)
);
CREATE INDEX IF NOT EXISTS idx_forecast_valid ON forecasts(valid_ts);

CREATE TABLE IF NOT EXISTS labels (
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    value REAL NOT NULL,
    note TEXT,
    PRIMARY KEY (ts, kind)
);

CREATE TABLE IF NOT EXISTS scores (
    ts REAL NOT NULL,
    target TEXT NOT NULL,
    horizon_s INTEGER NOT NULL,
    mae REAL, rmse REAL, bias REAL,
    mae_persistence REAL, skill REAL, coverage REAL, n INTEGER,
    PRIMARY KEY (ts, target, horizon_s)
);

CREATE TABLE IF NOT EXISTS events (
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=20.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------ writes

    def insert_telemetry(self, row: Dict[str, Any], tier: int = TIER_RAW) -> None:
        payload = {c: float(row.get(c)) if row.get(c) is not None else None for c in COLUMNS}
        payload["tier"] = tier
        cols = ", ".join(payload.keys())
        marks = ", ".join("?" for _ in payload)
        with self._conn() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO telemetry ({cols}) VALUES ({marks})",
                list(payload.values()),
            )

    def insert_forecast(self, issued_ts: float, horizon_s: int, target: str,
                        mu: float, lo: float, hi: float, model: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO forecasts "
                "(issued_ts, valid_ts, horizon_s, target, mu, lo, hi, model) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (issued_ts, issued_ts + horizon_s, horizon_s, target,
                 float(mu), float(lo), float(hi), model),
            )

    def insert_label(self, ts: float, kind: str, value: float, note: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO labels (ts, kind, value, note) VALUES (?,?,?,?)",
                (ts, kind, float(value), note),
            )

    def insert_score(self, ts: float, target: str, horizon_s: int, **kw) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO scores "
                "(ts, target, horizon_s, mae, rmse, bias, mae_persistence, skill, coverage, n) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts, target, horizon_s, kw.get("mae"), kw.get("rmse"), kw.get("bias"),
                 kw.get("mae_persistence"), kw.get("skill"), kw.get("coverage"), kw.get("n")),
            )

    def log_event(self, kind: str, severity: str, detail: str, ts: Optional[float] = None) -> None:
        with self._conn() as conn:
            conn.execute("INSERT INTO events (ts, kind, severity, detail) VALUES (?,?,?,?)",
                         (ts or time.time(), kind, severity, detail))

    # ------------------------------------------------------------- reads

    def all_for_recompute(self) -> Dict[str, np.ndarray]:
        """Every stored row's *raw* inputs, oldest first.

        Only the columns a re-derivation actually needs. The raw sensor values
        are never overwritten, which is precisely what makes recomputation
        possible after a calibration changes k or the humidity offset.
        """
        cols = ["ts", "temp_raw", "cpu_temp", "hum", "press"]
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(cols)} FROM telemetry ORDER BY ts ASC").fetchall()
        if not rows:
            return {c: np.empty(0) for c in cols}
        arr = np.array(rows, dtype=object)
        out = {}
        for i, c in enumerate(cols):
            out[c] = np.array([np.nan if v is None else float(v) for v in arr[:, i]],
                              dtype=float)
        return out

    def apply_recompute(self, ts: np.ndarray, updates: Dict[str, np.ndarray],
                        chunk: int = 2000) -> int:
        """Write recomputed derived columns back, in chunks.

        Chunked because a year of tiered history is a six-figure row count and a
        single statement would hold the whole parameter list in memory on a
        512 MB board.
        """
        names = list(updates.keys())
        sql = (f"UPDATE telemetry SET {', '.join(n + ' = ?' for n in names)} "
               f"WHERE ts = ?")
        n_written = 0
        with self._conn() as conn:
            for start in range(0, ts.size, chunk):
                stop = min(start + chunk, ts.size)
                batch = [
                    tuple(
                        [None if not np.isfinite(updates[n][i]) else float(updates[n][i])
                         for n in names] + [float(ts[i])]
                    )
                    for i in range(start, stop)
                ]
                conn.executemany(sql, batch)
                n_written += len(batch)
        return n_written

    def window(self, hours: float, columns: Optional[Iterable[str]] = None) -> Dict[str, np.ndarray]:
        """Return the last `hours` of telemetry as column arrays, oldest first."""
        cols = list(columns) if columns else COLUMNS
        since = time.time() - hours * 3600.0
        with self._conn() as conn:
            cur = conn.execute(
                f"SELECT {', '.join(cols)} FROM telemetry WHERE ts >= ? ORDER BY ts ASC",
                (since,),
            )
            rows = cur.fetchall()
        if not rows:
            return {c: np.empty(0, dtype=float) for c in cols}
        arr = np.array([[r[c] if r[c] is not None else np.nan for c in cols] for r in rows],
                       dtype=float)
        return {c: arr[:, i] for i, c in enumerate(cols)}

    # ------------------------------------------------- historical access

    @staticmethod
    def auto_bucket(start: float, end: float, target_points: int = 700) -> int:
        """Pick a sensible aggregation bucket for a requested span.

        The browser cannot draw more than about a thousand points usefully
        and the Pi should not serialise more than it must, so the bucket
        grows with the span. Snapped to familiar durations so the x-axis
        reads in round numbers rather than 437-second increments.
        """
        span = max(float(end) - float(start), 1.0)
        raw = span / max(int(target_points), 1)
        ladder = [30, 60, 120, 300, 600, 900, 1800, 3600, 7200,
                  10800, 21600, 43200, 86400, 604800]
        for step in ladder:
            if raw <= step:
                return step
        return ladder[-1]

    def range_series(self, start: float, end: float,
                     bucket_s: Optional[int] = None) -> Dict[str, Any]:
        """Bucket-aggregated telemetry between two epoch timestamps.

        Aggregation happens in SQLite rather than numpy: pulling 90 days of
        rows into Python to average them would cost more memory than the
        Zero 2 W has to spare. Min and max travel alongside the mean so the
        UI can shade a true range band instead of implying the mean was the
        whole story.
        """
        start, end = float(start), float(end)
        if end <= start:
            return {"n": 0, "bucket_s": 0, "series": {}}
        bucket = int(bucket_s or self.auto_bucket(start, end))

        # The alias must not be a bare single letter: the telemetry table has
        # r, g and b colour columns, and SQLite resolves an unqualified name in
        # GROUP BY to a real column before a result alias. `GROUP BY b` silently
        # grouped by the blue channel and returned one row per sample while
        # cheerfully reporting the requested bucket size.
        sql = f"""
            SELECT CAST(ts / {bucket} AS INTEGER) * {bucket} AS bucket_ts,
                   AVG(temp_smooth)  AS temp,  MIN(temp_smooth) AS temp_lo,
                   MAX(temp_smooth)  AS temp_hi,
                   AVG(hum_smooth)   AS hum,   MIN(hum_smooth)  AS hum_lo,
                   MAX(hum_smooth)   AS hum_hi,
                   AVG(press_slp)    AS press, MIN(press_slp)   AS press_lo,
                   MAX(press_slp)    AS press_hi,
                   AVG(dew_c)        AS dew,   AVG(lux)         AS lux,
                   AVG(temp_rate)    AS temp_rate,
                   AVG(press_rate)   AS press_rate,
                   AVG(cpu_temp)     AS cpu,   COUNT(*)         AS n
            FROM telemetry
            WHERE ts >= ? AND ts <= ?
            GROUP BY bucket_ts ORDER BY bucket_ts ASC
        """
        with self._conn() as conn:
            rows = conn.execute(sql, (start, end)).fetchall()
        if not rows:
            return {"n": 0, "bucket_s": bucket, "series": {}}

        keys = ["temp", "temp_lo", "temp_hi", "hum", "hum_lo", "hum_hi",
                "press", "press_lo", "press_hi", "dew", "lux",
                "temp_rate", "press_rate", "cpu", "n"]
        out: Dict[str, list] = {"ts": [float(r["bucket_ts"]) for r in rows]}
        for k in keys:
            out[k] = [r[k] for r in rows]
        return {"n": len(rows), "bucket_s": bucket,
                "start": start, "end": end, "series": out}

    def daily_summary(self, start: float, end: float) -> List[Dict[str, Any]]:
        """Per-calendar-day extremes and means, in the station's local time.

        Local time, not UTC: a `daily minimum` that straddles midnight in
        the wrong timezone is the kind of quiet wrongness nobody notices
        until they compare against the Met Office and lose an afternoon.
        """
        sql = """
            SELECT date(ts, 'unixepoch', 'localtime') AS day,
                   MIN(ts) AS first_ts, MAX(ts) AS last_ts, COUNT(*) AS n,
                   MIN(temp_smooth) AS temp_min, MAX(temp_smooth) AS temp_max,
                   AVG(temp_smooth) AS temp_mean,
                   MIN(hum_smooth)  AS hum_min,  MAX(hum_smooth)  AS hum_max,
                   AVG(hum_smooth)  AS hum_mean,
                   MIN(press_slp)   AS press_min, MAX(press_slp)  AS press_max,
                   AVG(press_slp)   AS press_mean,
                   AVG(dew_c) AS dew_mean, MAX(lux) AS lux_max
            FROM telemetry
            WHERE ts >= ? AND ts <= ?
            GROUP BY day ORDER BY day DESC
        """
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, (float(start), float(end))).fetchall()]

    def extremes(self) -> Dict[str, Any]:
        """All-time records held by the station, each with when it happened."""
        pairs = [
            ("temp_max", "temp_smooth", "DESC"), ("temp_min", "temp_smooth", "ASC"),
            ("hum_max", "hum_smooth", "DESC"), ("hum_min", "hum_smooth", "ASC"),
            ("press_max", "press_slp", "DESC"), ("press_min", "press_slp", "ASC"),
            ("dew_max", "dew_c", "DESC"), ("dew_min", "dew_c", "ASC"),
            ("rate_rise", "press_rate", "DESC"), ("rate_fall", "press_rate", "ASC"),
        ]
        # Physical sanity bounds. A Kalman filter's rate estimate is garbage
        # for the first few samples after it initialises, which happens on
        # every restart, and an unfiltered MAX() will faithfully enshrine that
        # transient as an all-time record of -37 hPa/h forever. The most
        # extreme real sea-level pressure changes on Earth are around
        # 10 hPa/h in an explosively deepening cyclone.
        bounds = {"press_rate": 10.0, "temp_rate": 25.0}

        out: Dict[str, Any] = {}
        with self._conn() as conn:
            for name, col, order in pairs:
                guard = ""
                if col in bounds:
                    guard = f" AND ABS({col}) <= {bounds[col]}"
                row = conn.execute(
                    f"SELECT ts, {col} AS v FROM telemetry "
                    f"WHERE {col} IS NOT NULL{guard} ORDER BY {col} {order} LIMIT 1"
                ).fetchone()
                out[name] = {"ts": row["ts"], "value": row["v"]} if row else None
            span = conn.execute("SELECT MIN(ts) AS a, MAX(ts) AS b, COUNT(*) AS n "
                                "FROM telemetry").fetchone()
        out["coverage"] = {"first_ts": span["a"], "last_ts": span["b"],
                           "rows": span["n"]}
        return out

    def iter_csv(self, start: float, end: float):
        """Yield CSV lines for export. Generator, so a year of history does
        not have to exist in memory at once on a 512 MB board."""
        cols = ["ts", "temp_smooth", "hum_smooth", "press_slp", "dew_c",
                "temp_rate", "press_rate", "cpu_temp", "lux", "tier"]
        yield "iso_time," + ",".join(cols) + "\n"
        with self._conn() as conn:
            cur = conn.execute(
                f"SELECT {', '.join(cols)} FROM telemetry "
                f"WHERE ts >= ? AND ts <= ? ORDER BY ts ASC",
                (float(start), float(end)),
            )
            while True:
                chunk = cur.fetchmany(500)
                if not chunk:
                    break
                for r in chunk:
                    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(r["ts"]))
                    vals = ["" if r[c] is None else
                            (f"{r[c]:.4f}" if isinstance(r[c], float) else str(r[c]))
                            for c in cols]
                    yield iso + "," + ",".join(vals) + "\n"

    def storage_stats(self) -> Dict[str, Any]:
        """Rows per resolution tier, so the retention policy is visible."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT tier, COUNT(*) AS n, MIN(ts) AS a, MAX(ts) AS b "
                "FROM telemetry GROUP BY tier ORDER BY tier"
            ).fetchall()
            page = conn.execute("PRAGMA page_count").fetchone()[0]
            size = conn.execute("PRAGMA page_size").fetchone()[0]
        names = {TIER_RAW: "raw", TIER_5MIN: "5 minute", TIER_HOUR: "hourly"}
        return {
            "tiers": [{"tier": r["tier"], "label": names.get(r["tier"], "?"),
                       "rows": r["n"], "first_ts": r["a"], "last_ts": r["b"]}
                      for r in rows],
            "bytes": int(page) * int(size),
        }

    def latest(self) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            cur = conn.execute("SELECT * FROM telemetry ORDER BY ts DESC LIMIT 1")
            row = cur.fetchone()
        return dict(row) if row else None

    def row_count(self) -> int:
        with self._conn() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0])

    def span_days(self) -> float:
        with self._conn() as conn:
            row = conn.execute("SELECT MIN(ts), MAX(ts) FROM telemetry").fetchone()
        if not row or row[0] is None:
            return 0.0
        return (row[1] - row[0]) / 86400.0

    def due_forecasts(self, now: Optional[float] = None) -> List[sqlite3.Row]:
        """Forecasts whose validity time has passed and can now be scored."""
        now = now or time.time()
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM forecasts WHERE valid_ts <= ? AND valid_ts >= ? ORDER BY valid_ts",
                (now, now - 7 * 86400),
            ).fetchall()

    def scorecard(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT s.* FROM scores s JOIN ("
                "  SELECT target, horizon_s, MAX(ts) AS mts FROM scores GROUP BY target, horizon_s"
                ") m ON s.target = m.target AND s.horizon_s = m.horizon_s AND s.ts = m.mts "
                "ORDER BY s.target, s.horizon_s"
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_events(self, limit: int = 25) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def labels(self, kind: str, hours: float = 24 * 30) -> Dict[str, np.ndarray]:
        since = time.time() - hours * 3600.0
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ts, value FROM labels WHERE kind = ? AND ts >= ? ORDER BY ts",
                (kind, since),
            ).fetchall()
        if not rows:
            return {"ts": np.empty(0), "value": np.empty(0)}
        return {
            "ts": np.array([r["ts"] for r in rows], dtype=float),
            "value": np.array([r["value"] for r in rows], dtype=float),
        }

    # -------------------------------------------------------- compaction

    def compact(self, raw_retention_days: float, five_min_retention_days: float) -> Dict[str, int]:
        """Fold old high-resolution rows into means. Returns rows removed per tier."""
        now = time.time()
        removed = {"raw": 0, "5min": 0}
        removed["raw"] = self._fold(TIER_RAW, TIER_5MIN, 300,
                                    now - raw_retention_days * 86400)
        removed["5min"] = self._fold(TIER_5MIN, TIER_HOUR, 3600,
                                     now - five_min_retention_days * 86400)
        with self._conn() as conn:
            conn.execute("PRAGMA incremental_vacuum")
        return removed

    def _fold(self, from_tier: int, to_tier: int, bucket_s: int, older_than: float) -> int:
        agg_cols = [c for c in COLUMNS if c != "ts"]
        select = ", ".join(f"AVG({c}) AS {c}" for c in agg_cols)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT CAST(ts / {bucket_s} AS INTEGER) * {bucket_s} AS bucket, {select} "
                f"FROM telemetry WHERE tier = ? AND ts < ? GROUP BY bucket",
                (from_tier, older_than),
            ).fetchall()
            if not rows:
                return 0
            cur = conn.execute("SELECT COUNT(*) FROM telemetry WHERE tier = ? AND ts < ?",
                               (from_tier, older_than))
            n_before = int(cur.fetchone()[0])
            conn.execute("DELETE FROM telemetry WHERE tier = ? AND ts < ?",
                         (from_tier, older_than))
            payload = [
                tuple([float(r["bucket"])] + [r[c] for c in agg_cols] + [to_tier])
                for r in rows
            ]
            marks = ", ".join("?" for _ in range(len(agg_cols) + 2))
            conn.executemany(
                f"INSERT OR REPLACE INTO telemetry (ts, {', '.join(agg_cols)}, tier) "
                f"VALUES ({marks})",
                payload,
            )
        return n_before - len(rows)


def resample(ts: np.ndarray, values: Dict[str, np.ndarray], grid_s: int,
             max_gap_grid: int = 3):
    """Bin irregular samples onto a regular grid, mean-aggregating each bin.

    Returns (grid_ts, {name: array}) with NaN in bins that had no data and
    linear interpolation across gaps no longer than `max_gap_grid` bins.
    Anything longer stays NaN so the learner never trains on invention.
    """
    if ts.size == 0:
        return np.empty(0), {k: np.empty(0) for k in values}

    start = np.floor(ts[0] / grid_s) * grid_s
    stop = np.floor(ts[-1] / grid_s) * grid_s
    grid = np.arange(start, stop + grid_s, grid_s, dtype=float)
    if grid.size == 0:
        return np.empty(0), {k: np.empty(0) for k in values}

    idx = np.clip(((ts - start) / grid_s).astype(int), 0, grid.size - 1)
    out = {}
    counts = np.bincount(idx, minlength=grid.size).astype(float)
    for name, arr in values.items():
        clean = np.nan_to_num(arr, nan=0.0)
        mask = (~np.isnan(arr)).astype(float)
        total = np.bincount(idx, weights=clean, minlength=grid.size)
        n = np.bincount(idx, weights=mask, minlength=grid.size)
        with np.errstate(invalid="ignore", divide="ignore"):
            binned = np.where(n > 0, total / np.maximum(n, 1e-9), np.nan)
        out[name] = _interp_short_gaps(binned, max_gap_grid)
    out["_count"] = counts
    return grid, out


def _interp_short_gaps(arr: np.ndarray, max_gap: int) -> np.ndarray:
    """Linear fill for runs of NaN up to `max_gap` long; leave longer runs alone."""
    a = arr.copy()
    isnan = np.isnan(a)
    if not isnan.any() or isnan.all():
        return a
    valid = np.flatnonzero(~isnan)
    filled = np.interp(np.arange(a.size), valid, a[valid])

    # find NaN runs and only accept the short ones
    edges = np.flatnonzero(np.diff(np.concatenate(([0], isnan.view(np.int8), [0]))))
    for start, stop in zip(edges[::2], edges[1::2]):
        if (stop - start) <= max_gap and start > 0 and stop < a.size:
            a[start:stop] = filled[start:stop]
    return a
