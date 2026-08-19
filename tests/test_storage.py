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

"""Schema migration.

The column-addition test is the important one. CREATE TABLE IF NOT EXISTS is a
no-op against a table that already exists, so every new entry in COLUMNS
reaches a fresh install and silently misses every station already running. It
then surfaces as an OperationalError inside insert_telemetry, which sits on the
sample loop, so a column addition takes a live station down rather than merely
leaving a gap in its record.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from ashvale.storage import COLUMNS, Store


def _cols(path: str) -> set[str]:
    with sqlite3.connect(path) as c:
        return {r[1] for r in c.execute("PRAGMA table_info(telemetry)")}


def test_fresh_database_has_every_declared_column(tmp_path):
    p = str(tmp_path / "fresh.db")
    Store(p)
    assert not [c for c in COLUMNS if c not in _cols(p)]


def test_migration_adds_a_new_column_without_touching_the_rows(tmp_path):
    """Simulates a station that has been running since before a column existed."""
    p = str(tmp_path / "old.db")
    legacy = [c for c in COLUMNS if c not in ("temp_h", "temp_p")]
    with sqlite3.connect(p) as c:
        c.execute(f"CREATE TABLE telemetry (ts REAL PRIMARY KEY, "
                  f"{', '.join(f'{x} REAL' for x in legacy if x != 'ts')}, "
                  f"tier INTEGER NOT NULL DEFAULT 0)")
        c.executemany("INSERT INTO telemetry (ts, temp_raw) VALUES (?, ?)",
                      [(float(i), 20.0 + i) for i in range(50)])

    assert "temp_h" not in _cols(p)
    store = Store(p)
    assert "temp_h" in _cols(p) and "temp_p" in _cols(p)

    with sqlite3.connect(p) as c:
        n = c.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]
        old = c.execute("SELECT temp_raw FROM telemetry WHERE ts = 7.0").fetchone()[0]
    assert n == 50, "migration must not lose rows"
    assert old == 27.0, "migration must not disturb existing values"

    # The point of the exercise: a write using the new columns must now work.
    store.insert_telemetry({"ts": 999.0, "temp_raw": 20.0, "temp_h": 20.6, "temp_p": 19.4})
    with sqlite3.connect(p) as c:
        row = c.execute("SELECT temp_h, temp_p FROM telemetry WHERE ts = 999.0").fetchone()
    assert row == (20.6, 19.4)


def test_migration_is_idempotent(tmp_path):
    p = str(tmp_path / "twice.db")
    Store(p)
    Store(p)
    Store(p)
    assert not [c for c in COLUMNS if c not in _cols(p)]


def test_both_thermometers_survive_a_round_trip(tmp_path):
    """temp_h and temp_p are logged so the self-heating gradient can be
    recovered later. They cannot be backfilled, so a silent drop is permanent."""
    p = str(tmp_path / "rt.db")
    store = Store(p)
    now = time.time()
    store.insert_telemetry({"ts": now, "temp_raw": 30.39, "temp_h": 30.973,
                            "temp_p": 29.810, "cpu_temp": 44.55})
    got = store.window(24.0, ["ts", "temp_h", "temp_p", "cpu_temp"])
    assert got["temp_h"][0] == 30.973
    assert got["temp_p"][0] == 29.810
    assert got["temp_h"][0] - got["temp_p"][0] == pytest.approx(1.163)
