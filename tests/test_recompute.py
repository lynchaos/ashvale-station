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

"""History re-derivation after a calibration.

The property that matters is idempotence. Recompute always starts from the
untouched raw columns, so running it twice must land in exactly the same place.
If it ever compounds, a user who clicks the button twice silently corrupts
their entire record.
"""

from __future__ import annotations

import sqlite3

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import ashvale.api as api  # noqa: E402
from ashvale.config import CONFIG  # noqa: E402


def _avg(col: str) -> float:
    with sqlite3.connect(CONFIG.storage.db_path) as c:
        return c.execute(f"SELECT round(avg({col}), 6) FROM telemetry").fetchone()[0]


def _snapshot() -> dict:
    """Per-row values keyed by timestamp.

    Deliberately not an aggregate. The station's sample loop is live under
    TestClient, so rows arrive between calls and any average over the whole
    table is a moving target. Comparing the rows present in both snapshots
    tests the property that actually matters.
    """
    with sqlite3.connect(CONFIG.storage.db_path) as c:
        return {r[0]: (r[1], r[2]) for r in
                c.execute("SELECT ts, hum_smooth, temp_smooth FROM telemetry")}


def _rows() -> int:
    with sqlite3.connect(CONFIG.storage.db_path) as c:
        return c.execute("SELECT count(*) FROM telemetry").fetchone()[0]


@pytest.fixture(scope="module")
def client():
    with TestClient(api.app) as c:
        yield c


def test_recompute_is_idempotent(client):
    """Running it twice must land in exactly the same place, row for row.

    It always starts from the untouched raw columns, so it cannot compound. If
    that ever breaks, a user clicking the button twice silently corrupts their
    whole record, which is why this is tested per row rather than on an average.
    """
    if _rows() == 0:
        pytest.skip("no history in the database")
    client.post("/api/recompute")
    first = _snapshot()
    client.post("/api/recompute")
    second = _snapshot()
    common = set(first) & set(second)
    assert common, "no overlapping rows to compare"
    differing = [ts for ts in common if first[ts] != second[ts]]
    assert not differing, f"{len(differing)} of {len(common)} rows changed on re-run"


def test_recompute_preserves_row_count(client):
    if _rows() == 0:
        pytest.skip("no history in the database")
    before = _rows()
    client.post("/api/recompute")
    assert _rows() == before


def test_recompute_tracks_the_current_offset(client):
    """Changing the calibration must move the whole history, not just new rows."""
    if _rows() == 0:
        pytest.skip("no history in the database")
    client.post("/api/calibrate/humidity", json={"reset": True})
    client.post("/api/recompute")
    base = _avg("hum_smooth")

    client.post("/api/calibrate/humidity", json={"reference_pct": 30.0})
    client.post("/api/recompute")
    shifted = _avg("hum_smooth")
    assert shifted != pytest.approx(base), "history did not follow the new offset"

    client.post("/api/calibrate/humidity", json={"reset": True})
    client.post("/api/recompute")
    assert _avg("hum_smooth") == pytest.approx(base, abs=0.5), "reset did not restore"


def test_calibration_logs_a_discontinuity_marker(client):
    client.post("/api/calibrate/humidity", json={"reference_pct": 55.0})
    kinds = [e["kind"] for e in client.get("/api/status").json()["events"]]
    assert "discontinuity" in kinds
    client.post("/api/calibrate/humidity", json={"reset": True})
