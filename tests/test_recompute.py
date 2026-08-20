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
    """Recompute must never drop a row.

    Asserted as "no fewer than before" rather than equality: the sample loop is
    live under TestClient and legitimately inserts rows mid-test. Equality here
    was flaky for that reason, and a flaky test is worse than no test because it
    trains you to ignore red.
    """
    if _rows() == 0:
        pytest.skip("no history in the database")
    before = _rows()
    result = client.post("/api/recompute").json()
    after = _rows()
    assert after >= before, f"rows lost: {before} -> {after}"
    assert result["rows"] >= before, "recompute touched fewer rows than existed"


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


# ------------------------------------------------------------ clock guard

def test_training_refuses_a_clock_that_has_not_been_set(tmp_path, monkeypatch):
    """The board has no RTC.

    A power cut without a network gives a clock somewhere in 1970 on the next
    boot. Solar elevation, the diurnal harmonics and a sample's position on the
    5-minute grid all then lie with total confidence, and unlike a gap in the
    record the damage cannot be spotted afterwards.
    """
    import time as _time

    from ashvale.config import load_config
    from ashvale.station import Station

    cfg = load_config()
    cfg.storage.db_path = str(tmp_path / "clock.db")
    st = Station(cfg)

    assert st.clock_sanity()["ok"], "a correct clock must pass"

    monkeypatch.setattr(_time, "time", lambda: 1000.0)      # 1970
    verdict = st.clock_sanity()
    assert not verdict["ok"]
    assert "2025" in verdict["reason"]
    result = st.train()
    assert result["trained"] is False
    # and specifically for the clock, not because the database is empty
    assert "2025" in result["reason"], result["reason"]


def test_training_refuses_a_clock_that_went_backwards(tmp_path, monkeypatch):
    """NTP stepping backwards past stored data is equally unusable."""
    import time as _time

    from ashvale.config import load_config
    from ashvale.station import Station

    cfg = load_config()
    cfg.storage.db_path = str(tmp_path / "back.db")
    st = Station(cfg)
    future = _time.time() + 7200.0
    st.store.insert_telemetry({"ts": future, "temp_raw": 20.0})

    verdict = st.clock_sanity()
    assert not verdict["ok"]
    assert "behind" in verdict["reason"]


# ------------------------------------------------------------ joystick

def test_joystick_left_and_right_record_rain_labels(tmp_path):
    """The button that fixes the precipitation model.

    Strong labels are the binding constraint on that head: 80 against thousands
    of proxy ones on a real station, because the only label control lives in a
    web page. Left is dry, right is wet.
    """
    import asyncio
    import sqlite3

    from ashvale.config import load_config
    from ashvale.station import Station

    cfg = load_config()
    cfg.storage.db_path = str(tmp_path / "stick.db")
    st = Station(cfg)
    st.sample_once()

    pending = [("left", "pressed"), ("right", "pressed"),
               ("up", "pressed"), ("right", "released")]

    def fake_events():
        out, pending[:] = list(pending), []
        return out

    st.board.stick_events = fake_events

    async def one_pass():
        task = asyncio.create_task(st._loop_joystick())
        await asyncio.sleep(0.6)
        st._stop.set()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(one_pass())

    with sqlite3.connect(cfg.storage.db_path) as c:
        rows = sorted(r[0] for r in c.execute("SELECT value FROM labels WHERE kind='rain'"))
    assert rows == [0.0, 1.0], f"expected one dry and one wet label, got {rows}"
    # 'up' is unbound and 'released' is not a press: neither may label anything.


def test_joystick_survives_a_board_with_no_hat(tmp_path):
    """The simulator path has no stick. The loop must not spin on exceptions."""
    from ashvale.config import load_config
    from ashvale.station import Station

    cfg = load_config()
    cfg.storage.db_path = str(tmp_path / "nohat.db")
    st = Station(cfg)
    assert st.board.stick_events() == []
    assert st.display is None


# ------------------------------------------------------- movement detection

def _moved_station(tmp_path, name):
    from ashvale.config import load_config
    from ashvale.station import Station

    cfg = load_config()
    cfg.storage.db_path = str(tmp_path / name)
    st = Station(cfg)
    st._started_at = 0.0            # long settled
    return st


def test_a_moved_board_marks_a_discontinuity_and_queues_a_retrain(tmp_path):
    """Measured on a real station: a move steps the temperature by a median of
    1.02 C against an ordinary fifteen minute change of 0.107 C. The heads
    carry 55 hours of memory, so an undeclared move contaminates two days.
    """
    st = _moved_station(tmp_path, "moved.db")
    base = 1.7554e9

    for i in range(20):             # sitting still, with realistic jitter
        st._check_moved(base + i, {"pitch": -64.05 + 1e-5 * i,
                                   "roll": 1.36, "temp_smooth": 24.0})
    assert not st.monitor.retrain_requested, "noise must not trigger a retrain"

    st._check_moved(base + 100, {"pitch": -53.5, "roll": 1.4, "temp_smooth": 24.0})
    assert st.monitor.retrain_requested, "a 10 degree move must queue a retrain"

    import sqlite3
    with sqlite3.connect(st.cfg.storage.db_path) as c:
        kinds = [r[0] for r in c.execute("SELECT kind FROM events")]
    assert "moved" in kinds and "discontinuity" in kinds


def test_restart_attitude_jump_is_not_mistaken_for_a_move(tmp_path):
    """RTIMULib restarts its fusion from a default attitude when SenseHat is
    reconstructed, which put an 18 degree step in the record on every service
    restart. Without the settle window every deploy looks like a move.
    """
    st = _moved_station(tmp_path, "restart.db")
    now = 1.7554e9
    st._started_at = now                       # just booted

    st._check_moved(now + 1, {"pitch": -46.0, "roll": 1.4, "temp_smooth": 24.0})
    st._check_moved(now + 60, {"pitch": -64.1, "roll": 1.4, "temp_smooth": 24.0})
    st._check_moved(now + 180, {"pitch": -46.0, "roll": 1.4, "temp_smooth": 24.0})
    assert not st.monitor.retrain_requested, "startup convergence must be ignored"

    # past the settle window, the same step is a real move
    st._check_moved(now + st.TILT_SETTLE_S + 10, {"pitch": -64.1, "roll": 1.4,
                                                  "temp_smooth": 24.0})
    assert st.monitor.retrain_requested


def test_yaw_and_compass_are_not_used_for_movement(tmp_path):
    """They depend on the magnetometer, which indoors measures the building."""
    st = _moved_station(tmp_path, "yaw.db")
    base = 1.7554e9
    st._check_moved(base, {"pitch": -64.0, "roll": 1.4, "yaw": 10.0,
                           "compass": 10.0, "temp_smooth": 24.0})
    st._check_moved(base + 30, {"pitch": -64.0, "roll": 1.4, "yaw": 300.0,
                                "compass": 300.0, "temp_smooth": 24.0})
    assert not st.monitor.retrain_requested, "a 290 degree yaw swing is not a move"
