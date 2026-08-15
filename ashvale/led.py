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

"""The 8x8 matrix as a forecast instrument, not a scrolling number.

Text on eight pixels is slow and, worse, it makes you wait for the one
value you wanted. So the display cycles through *glyphs* that are
readable at a glance from across a room:

  temperature   scrolled with a heat-mapped colour, as before
  humidity      scrolled with a moisture-band colour
  pressure      a trend arrow whose colour encodes the Zambretti class
                and whose brightness encodes tendency magnitude
  rain          a filled column bar, 0 to 8 pixels, of rain probability
  forecast      a 3-hour temperature delta as a rising or falling wedge
  alert         a red pulse if a sensor is faulted or drift fired

Design constraint: never call `show_message` while an alert is pending,
because a 6-second scroll is a 6-second delay on the only frame that
matters.
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Sequence, Tuple

OFF = (0, 0, 0)


def temp_colour(temp_c: float) -> List[int]:
    if temp_c <= 15.0:
        return [0, 150, 255]
    if temp_c <= 21.0:
        return [0, 255, 180]
    if temp_c <= 25.0:
        return [70, 255, 0]
    if temp_c <= 28.0:
        return [255, 190, 0]
    if temp_c <= 32.0:
        return [255, 90, 0]
    return [255, 20, 20]


def humidity_colour(rh: float) -> List[int]:
    if rh < 35.0:
        return [255, 180, 50]
    if rh <= 60.0:
        return [0, 210, 255]
    return [0, 100, 255]


CONDITION_COLOUR = {
    "settled": (0, 220, 140), "fine": (90, 230, 60), "fair": (200, 230, 40),
    "changeable": (255, 190, 0), "unsettled": (255, 120, 0),
    "rain": (0, 140, 255), "wet": (0, 90, 255), "stormy": (255, 40, 60),
}

# 8x8 bitmaps: '#' is lit, anything else is off


def _mask(rows: Sequence[str]) -> List[List[int]]:
    return [[1 if ch == "#" else 0 for ch in row.ljust(8, ".")[:8]] for row in rows]


ARROW_UP = _mask([
    "...##...",
    "..####..",
    ".##..##.",
    "##.##.##",
    "...##...",
    "...##...",
    "...##...",
    "...##...",
])

ARROW_DOWN = _mask([
    "...##...",
    "...##...",
    "...##...",
    "...##...",
    "##.##.##",
    ".##..##.",
    "..####..",
    "...##...",
])

ARROW_FLAT = _mask([
    "........",
    "........",
    "....#...",
    "########",
    "########",
    "....#...",
    "........",
    "........",
])

DROP = _mask([
    "...##...",
    "...##...",
    "..####..",
    ".######.",
    "########",
    "########",
    ".######.",
    "..####..",
])

BANG = _mask([
    "...##...",
    "...##...",
    "...##...",
    "...##...",
    "...##...",
    "........",
    "...##...",
    "...##...",
])


def render(mask: List[List[int]], colour: Tuple[int, int, int],
           dim: float = 1.0) -> List[Tuple[int, int, int]]:
    c = tuple(int(max(0, min(255, v * dim))) for v in colour)
    return [c if cell else OFF for row in mask for cell in row]


def bar(fraction: float, colour: Tuple[int, int, int],
        background: Tuple[int, int, int] = (12, 12, 20)) -> List[Tuple[int, int, int]]:
    """Bottom-up column bar across the full 8x8, 1/64 resolution."""
    lit = int(round(max(0.0, min(1.0, fraction)) * 64))
    pixels = [background] * 64
    count = 0
    for row in range(7, -1, -1):
        for col in range(8):
            if count < lit:
                pixels[row * 8 + col] = colour
                count += 1
    return pixels


class LedDisplay:
    """Async display worker. Owns the matrix, reads station state, nothing else."""

    def __init__(self, station, cycle_s: float = 0.4):
        self.station = station
        self.cycle_s = float(cycle_s)
        self.enabled = True
        self._stop = asyncio.Event()
        self._task = None
        self.frame_name = "idle"

    # ------------------------------------------------------------ frames

    async def _alert_frame(self) -> bool:
        health = self.station.monitor.health.overall
        drift = self.station.monitor.retrain_requested
        if health == "ok" and not drift:
            return False
        colour = (255, 40, 40) if health == "fault" else (255, 150, 0)
        self.frame_name = "alert"
        for pulse in (1.0, 0.25, 1.0, 0.25):
            self.station.board.set_pixels(render(BANG, colour, pulse))
            await asyncio.sleep(0.22)
        self.station.board.clear()
        return True

    async def _pressure_frame(self) -> None:
        live = self.station.live
        precip = self.station.precip_bundle or {}
        rate = float(live.get("press_rate", 0.0) or 0.0)
        condition = precip.get("condition", "changeable")
        colour = CONDITION_COLOUR.get(condition, (200, 200, 200))
        magnitude = min(abs(rate) / 1.2, 1.0)
        dim = 0.25 + 0.75 * magnitude

        if rate > 0.15:
            mask = ARROW_UP
        elif rate < -0.15:
            mask = ARROW_DOWN
        else:
            mask = ARROW_FLAT
        self.frame_name = "pressure-trend"
        self.station.board.set_pixels(render(mask, colour, dim))
        await asyncio.sleep(2.0)
        self.station.board.clear()

    async def _rain_frame(self) -> None:
        p = float((self.station.precip_bundle or {}).get("rain_probability", 0.0))
        self.frame_name = "rain-probability"
        if p < 0.12:
            return
        self.station.board.set_pixels(bar(p, (40, 130, 255)))
        await asyncio.sleep(1.6)
        self.station.board.set_pixels(render(DROP, (40, 130, 255), 0.6 + 0.4 * p))
        await asyncio.sleep(1.0)
        self.station.board.clear()

    async def _forecast_frame(self) -> None:
        bundle = self.station.forecast_bundle or {}
        series = (bundle.get("targets", {}).get("temperature") or [])
        target = next((s for s in series if s["horizon_s"] == 10800), None)
        if target is None:
            return
        delta = float(target["delta"])
        self.frame_name = "temp-3h-delta"
        colour = (255, 120, 0) if delta > 0 else (0, 170, 255)
        mask = ARROW_UP if delta > 0.2 else ARROW_DOWN if delta < -0.2 else ARROW_FLAT
        self.station.board.set_pixels(render(mask, colour, 0.35 + min(abs(delta) / 3.0, 0.65)))
        await asyncio.sleep(1.6)
        self.station.board.clear()

    async def _scroll_frames(self) -> None:
        live = self.station.live
        temp = live.get("temp_smooth")
        hum = live.get("hum_smooth")
        press = live.get("press_slp")
        if temp is not None:
            self.frame_name = "temperature"
            self.station.board.show_message(f"{temp:.1f}C", 0.065, temp_colour(temp))
            await asyncio.sleep(self.cycle_s)
        if hum is not None:
            self.frame_name = "humidity"
            self.station.board.show_message(f"{hum:.0f}%", 0.065, humidity_colour(hum))
            await asyncio.sleep(self.cycle_s)
        if press is not None:
            self.frame_name = "pressure"
            self.station.board.show_message(f"{press:.0f}", 0.065, [180, 80, 255])
            await asyncio.sleep(self.cycle_s)

    # -------------------------------------------------------------- loop

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.enabled or not self.station.live:
                    await asyncio.sleep(1.0)
                    continue
                if await self._alert_frame():
                    continue
                await self._scroll_frames()
                await self._pressure_frame()
                await self._forecast_frame()
                await self._rain_frame()
            except Exception:
                await asyncio.sleep(2.0)

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self.station.board.clear()
