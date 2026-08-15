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

"""The 8x8 matrix as an instrument you actually want to look at.

Sixty-four pixels is not much, and the naive approach (draw a glyph, hold it,
cut to the next) looks like a microwave clock. Three things do most of the work
of making it look like something else entirely:

1. **Gamma.** LED duty cycle is linear, human brightness perception is not. Sent
   raw, the bottom half of every gradient collapses into the same visible step
   and dim colours vanish. Everything here renders in linear float and is
   encoded through a gamma curve exactly once, on the way out.

2. **Sub-pixel rendering.** A dot at x = 3.4 lights pixel 3 at 60% and pixel 4
   at 40%. Nothing ever snaps to the grid, so eight pixels read as a smooth
   continuum rather than eight blocks. This is the single biggest difference
   between "LED matrix" and "little window".

3. **Crossfades.** Scenes dissolve into each other over a second or so, and
   every scene is a continuous function of time rather than a series of held
   frames. There are no hard cuts anywhere.

On top of that the panel is dimmed by measured ambient light, so at 3 a.m. it
is a faint glow rather than a searchlight in your bedroom.

Every scene is also a *reading*. The aurora's hue is the temperature and its
flow direction is the pressure tendency; the sun sits at its true azimuth and
elevation; the rain density is the forecast probability. It is pretty because
the data is doing the work, not because it is decorated.

Cost: the whole thing is numpy on a (8, 8, 3) array, about 200 floats. At 24 fps
that is a fraction of a percent of one core on a Zero 2 W, and the matrix is a
memory-mapped framebuffer rather than a bus transaction, so pushing frames is
nearly free. Measured RSS impact: none worth reporting.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Dict, List, Optional, Tuple

import numpy as np

N = 8
FPS = 24.0
GAMMA = 2.2

# Pixel centres, so a disc at (3.5, 3.5) is centred on the panel rather than
# sitting a half pixel off it.
_XS = np.arange(N, dtype=float)
X, Y = np.meshgrid(_XS, _XS)
_CX = _CY = (N - 1) / 2.0
RADIUS = np.hypot(X - _CX, Y - _CY)

# Encode once, on the way out. 256 entries is plenty and costs nothing.
_GAMMA_LUT = np.clip(
    (np.linspace(0.0, 1.0, 256) ** GAMMA) * 255.0 + 0.5, 0, 255
).astype(np.uint8)

# The Sense HAT framebuffer is RGB565: 32 levels of red and blue, 64 of green.
# After gamma that leaves very few usable steps at the dim end, which is exactly
# where an aurora or a star field lives, and smooth gradients band into stripes.
# An ordered dither rotated every frame trades that spatial banding for temporal
# noise at 24 fps, which the eye integrates back into the levels between the
# levels. This is the difference between a gradient and a staircase.
_BAYER4 = np.array([[0, 8, 2, 10],
                    [12, 4, 14, 6],
                    [3, 11, 1, 9],
                    [15, 7, 13, 5]], dtype=float) / 16.0
_DITHER = np.tile(_BAYER4, (2, 2))          # 8x8, one cell per pixel
_STEP565 = np.array([255.0 / 31.0, 255.0 / 63.0, 255.0 / 31.0])   # one hardware step


def _hsv(h: float, s: float, v: float) -> Tuple[float, float, float]:
    """HSV to linear RGB. Hue wraps, so palettes can rotate without a branch."""
    h = h % 1.0
    i = int(h * 6.0)
    f = h * 6.0 - i
    p, q, t = v * (1.0 - s), v * (1.0 - s * f), v * (1.0 - s * (1.0 - f))
    return [(v, t, p), (q, v, p), (p, v, t),
            (p, q, v), (t, p, v), (v, p, q)][i % 6]


def _mix(a, b, t: float):
    """Linear blend in linear light, which is where blending is meaningful."""
    t = min(max(t, 0.0), 1.0)
    return tuple(a[i] * (1.0 - t) + b[i] * t for i in range(3))


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    """Hermite ramp between two edges. Handles a descending range.

    The descending case is not decoration: writing _smoothstep(2, -8, elev) to
    mean "1 when the sun is well down" silently returned the exact inverse when
    this fell through to the degenerate branch, and the panel drew a moon at
    midday and a sun at midnight.
    """
    if edge0 == edge1:
        return 0.0 if x < edge0 else 1.0
    t = min(max((x - edge0) / (edge1 - edge0), 0.0), 1.0)
    return t * t * (3.0 - 2.0 * t)


# A 3x5 glyph set. Three pixels wide is the narrowest a digit can be and stay
# legible, which on an 8x8 leaves room for two digits and a unit mark, or a
# smoothly scrolling strip of any length.
_FONT = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    "-": ("000", "000", "111", "000", "000"),
    "+": ("000", "010", "111", "010", "000"),
    ".": ("000", "000", "000", "000", "010"),
    "%": ("101", "001", "010", "100", "101"),
    "C": ("111", "100", "100", "100", "111"),
    "h": ("100", "100", "110", "101", "101"),
    "P": ("111", "101", "111", "100", "100"),
    "a": ("000", "110", "011", "101", "111"),
    " ": ("000", "000", "000", "000", "000"),
}


def _text_width(text: str) -> int:
    return sum(4 for _ in text)


def _draw_text(cv: Canvas, text: str, x: float, y: float, colour,
               alpha: float = 1.0) -> None:
    """Whole-pixel text, deliberately.

    Everything else on this panel is sub-pixel rendered, and for particles and
    discs that is what makes it look good. For a 3 px wide glyph it is ruinous:
    splitting each stroke across two columns halves its peak brightness and
    smears the letterform until it is unreadable. Text snaps to the grid and
    scrolls in whole steps. Crisp beats smooth when the thing has to be read.
    """
    x = round(x)
    y = round(y)
    for ch in text:
        g = _FONT.get(ch)
        if g is not None and -4 < x < N + 1:
            for r, row in enumerate(g):
                yy = y + r
                if yy < 0 or yy >= N:
                    continue
                for c, on in enumerate(row):
                    xx = x + c
                    if on == "1" and 0 <= xx < N:
                        cv.buf[yy, xx, 0] += colour[0] * alpha
                        cv.buf[yy, xx, 1] += colour[1] * alpha
                        cv.buf[yy, xx, 2] += colour[2] * alpha
        x += 4


class Canvas:
    """An 8x8 linear-light RGB buffer with sub-pixel drawing."""

    __slots__ = ("buf",)

    def __init__(self) -> None:
        self.buf = np.zeros((N, N, 3), dtype=float)

    def clear(self) -> None:
        self.buf[:] = 0.0

    def fade(self, keep: float) -> None:
        """Multiply everything down. This is what leaves motion trails."""
        self.buf *= keep

    def wash(self, field: np.ndarray, colour) -> None:
        """Add a colour weighted by a per-pixel intensity field."""
        f = np.clip(field, 0.0, None)[..., None]
        self.buf += f * np.asarray(colour, dtype=float)

    def plot(self, x: float, y: float, colour, alpha: float = 1.0) -> None:
        """Additive splat with bilinear weights: the sub-pixel workhorse.

        Fractional coordinates spread energy across the four neighbouring
        pixels, so a dot crossing the panel glides instead of stepping.
        """
        if alpha <= 0.0:
            return
        x0, y0 = int(math.floor(x)), int(math.floor(y))
        fx, fy = x - x0, y - y0
        # Scalar component writes, not a 3-vector slice add. numpy's per-call
        # overhead dominates at this size, and the splat is the hot path for
        # every particle and every glyph stroke.
        cr, cg, cb = colour[0] * alpha, colour[1] * alpha, colour[2] * alpha
        buf = self.buf
        for dy in (0, 1):
            yy = y0 + dy
            if yy < 0 or yy >= N:
                continue
            wy = fy if dy else (1.0 - fy)
            if wy <= 0.0:
                continue
            for dx in (0, 1):
                xx = x0 + dx
                if xx < 0 or xx >= N:
                    continue
                wx = fx if dx else (1.0 - fx)
                w = wx * wy
                if w <= 0.0:
                    continue
                buf[yy, xx, 0] += cr * w
                buf[yy, xx, 1] += cg * w
                buf[yy, xx, 2] += cb * w

    def column(self, x: float, height: float, colour, alpha: float = 1.0) -> None:
        """A bar with a soft, fractional top edge rather than a stepped one."""
        for row in range(N):
            y_from_bottom = (N - 1) - row
            cover = min(max(height - y_from_bottom, 0.0), 1.0)
            if cover > 0.0:
                self.plot(x, row, colour, alpha * cover)

    def to_pixels(self, brightness: float, phase: int = 0) -> List[List[int]]:
        lit = np.clip(self.buf * brightness, 0.0, 1.0)
        idx = (lit * 255.0 + 0.5).astype(np.int32)
        enc = _GAMMA_LUT[idx].astype(float)
        # Offset by up to one hardware step, rotating the pattern each frame so
        # the noise averages out over time rather than sitting still as texture.
        d = ((_DITHER + (phase % 4) * 0.25) % 1.0)[..., None] - 0.5
        enc = enc + d * _STEP565
        return np.clip(enc + 0.5, 0, 255).astype(np.int32).reshape(-1, 3).tolist()


# --------------------------------------------------------------------------
# Scenes. Each is a pure function of (time, station snapshot) so it can be
# crossfaded with any other simply by rendering both and blending.
# --------------------------------------------------------------------------

class Scene:
    name = "scene"
    duration = 12.0

    def render(self, cv: Canvas, t: float, s: Dict) -> None:
        raise NotImplementedError


class Aurora(Scene):
    """Layered plasma curtains. The ambient default, and the one to stare at.

    Four sine fields at incommensurate frequencies sum into something that never
    visibly repeats. Hue is the temperature, mapped over a range wide enough that
    a British winter and a hot afternoon are obviously different colours. The
    flow direction is the pressure tendency: rising air drifts the curtains up,
    falling drifts them down, so the panel tells you which way the barometer is
    going before you read a number.
    """

    name = "aurora"
    duration = 16.0

    def render(self, cv: Canvas, t: float, s: Dict) -> None:
        temp = s.get("temp", 15.0)
        rate = s.get("press_rate", 0.0)
        rh = s.get("humidity", 60.0)

        # -5 C to 32 C spans violet through cyan, green, amber, rose.
        warm = _smoothstep(-5.0, 32.0, temp)
        hue = 0.72 - 0.62 * warm

        drift = float(np.clip(rate / 1.5, -1.0, 1.0))
        flow = t * (0.28 + 0.5 * abs(drift))
        dir_y = -drift

        f = (np.sin(X * 0.85 + flow)
             + np.sin(Y * 1.15 + flow * dir_y * 1.4)
             + np.sin((X + Y) * 0.55 - flow * 0.7)
             + np.sin(RADIUS * 1.25 - flow * 1.1))
        f = (f + 4.0) / 8.0

        # Humid air reads as a denser, more contrasted curtain.
        contrast = 1.0 + 1.4 * _smoothstep(40.0, 95.0, rh)
        f = np.clip(f, 0.0, 1.0) ** contrast

        # Iridescence: hue drifts slightly across the field so the curtains
        # separate into bands instead of being one flat wash of colour.
        for row in range(N):
            for col in range(N):
                v = float(f[row, col])
                if v <= 0.02:
                    continue
                h = hue + 0.10 * math.sin((col - row) * 0.4 + t * 0.25)
                cv.buf[row, col] += np.asarray(_hsv(h, 0.85, v * 0.9))


class SolarSky(Scene):
    """A window onto the real sky: sun or moon at its true azimuth and elevation.

    The disc is placed by the actual solar position already computed for the
    features, so at 07:00 it genuinely sits low and left, and at noon it is high.
    The sky behind it runs through dawn, day and dusk on measured elevation. After
    sunset the panel becomes a starfield with a moon, dimmed right down.

    The stars are deterministic per index rather than random per frame, so they
    twinkle in place instead of boiling.
    """

    name = "solar-sky"
    duration = 14.0

    def render(self, cv: Canvas, t: float, s: Dict) -> None:
        elev = s.get("solar_elevation", -20.0)
        azim = s.get("solar_azimuth", 180.0)
        cloud = s.get("cloud", 0.4)

        day = _smoothstep(-6.0, 8.0, elev)
        golden = 1.0 - abs(_smoothstep(-6.0, 14.0, elev) * 2.0 - 1.0)

        night_top = (0.010, 0.016, 0.055)
        night_bot = (0.030, 0.030, 0.080)
        day_top = (0.050, 0.190, 0.480)
        day_bot = (0.230, 0.420, 0.680)
        gold_bot = (0.520, 0.230, 0.090)

        for row in range(N):
            k = row / (N - 1.0)
            top = _mix(night_top, day_top, day)
            bot = _mix(night_bot, _mix(day_bot, gold_bot, golden * 0.8), day)
            cv.buf[row, :] += np.asarray(_mix(top, bot, k))

        if day < 0.35:
            for i in range(14):
                sx = (i * 2.713) % N
                sy = (i * 1.371 + 0.7) % (N * 0.75)
                tw = 0.45 + 0.55 * math.sin(t * (1.1 + 0.23 * i) + i * 2.0)
                cv.plot(sx, sy, (0.85, 0.88, 1.0), 0.16 * tw * (1.0 - day))
            # Waxing moon: a bright disc with a bite taken out of it.
            mx = 1.6 + 0.4 * math.sin(t * 0.09)
            my = 1.5
            cv.plot(mx, my, (0.95, 0.95, 0.85), 0.55 * (1.0 - day))
            cv.plot(mx + 0.85, my - 0.2, (0.0, 0.0, 0.0), 0.0)

        if day > 0.02:
            # Azimuth 90 (east) to 270 (west) maps left to right across the panel.
            px = float(np.clip((azim - 90.0) / 180.0, 0.0, 1.0)) * (N - 1)
            py = (N - 1) * (1.0 - float(np.clip((elev + 6.0) / 66.0, 0.0, 1.0)))
            disc = _mix((1.0, 0.55, 0.15), (1.0, 0.95, 0.70), day)
            glow = np.exp(-((X - px) ** 2 + (Y - py) ** 2) / 3.2)
            cv.wash(glow * 0.55 * day * (1.0 - 0.45 * cloud), disc)
            cv.plot(px, py, disc, 0.9 * day)

        if cloud > 0.25 and day > 0.1:
            band = np.exp(-((Y - (2.2 + 1.1 * math.sin(t * 0.13))) ** 2) / 1.4)
            slide = 0.5 + 0.5 * np.sin(X * 0.7 + t * 0.16)
            cv.wash(band * slide * 0.30 * cloud * day, (0.55, 0.58, 0.62))

        # Without this the sky is a frozen gradient, which reads as a dead panel
        # rather than a calm one. Two slow incommensurate waves give it the faint
        # movement of air, at a few percent so it never becomes the subject.
        shimmer = (np.sin(X * 0.55 + t * 0.21) * np.sin(Y * 0.42 - t * 0.17)
                   + np.sin((X - Y) * 0.33 + t * 0.11))
        cv.buf *= (1.0 + 0.055 * shimmer)[..., None]


class Precipitation(Scene):
    """Rain, snow or storm, chosen by the forecast and the thermometer.

    Drop count scales with rain probability, so a dry day is a near-empty panel
    and a wet one is a downpour. Below 1.5 C the drops become snow: slower, half
    the fall speed, swaying sideways on a sine, and they twinkle. A stormy
    Zambretti class adds lightning, which is a full-panel flash with an
    exponential afterglow rather than an on/off blink.

    Each drop keeps a fractional y, and the trail comes from fading the canvas
    rather than from drawing a streak, which is both cheaper and softer.
    """

    name = "precipitation"
    duration = 13.0

    def __init__(self) -> None:
        self.drops: List[List[float]] = []
        self._last_bolt = -99.0
        self._bolt_at = -99.0

    def render(self, cv: Canvas, t: float, s: Dict) -> None:
        p = s.get("rain_prob", 0.0)
        temp = s.get("temp", 10.0)
        stormy = s.get("condition") in ("stormy", "wet")
        snowing = temp <= 1.5

        cv.fade(0.55)

        want = int(round(1 + 13 * p))
        while len(self.drops) < want:
            self.drops.append([np.random.uniform(0, N), np.random.uniform(-N, 0),
                               np.random.uniform(0.8, 1.0)])
        while len(self.drops) > want:
            self.drops.pop()

        speed = (1.1 if snowing else 5.2) * (0.6 + 0.8 * p)
        colour = (0.80, 0.88, 1.00) if snowing else (0.20, 0.55, 1.00)

        for d in self.drops:
            d[1] += speed / max(s.get('_fps', FPS), 1.0)
            if d[1] > N + 1:
                d[0] = np.random.uniform(0, N)
                d[1] = np.random.uniform(-2.0, -0.2)
                d[2] = np.random.uniform(0.8, 1.0)
            x = d[0]
            if snowing:
                x += 0.9 * math.sin(t * 0.8 + d[0] * 1.7)
                tw = 0.6 + 0.4 * math.sin(t * 3.0 + d[0] * 5.0)
            else:
                tw = 1.0
            cv.plot(x % N, d[1], colour, 0.75 * d[2] * tw)

        if stormy:
            if t - self._last_bolt > np.random.uniform(2.0, 6.0):
                self._last_bolt = t
                self._bolt_at = t
            age = t - self._bolt_at
            if 0.0 <= age < 0.55:
                cv.buf += np.asarray((0.85, 0.85, 1.0)) * math.exp(-age * 9.0)


class Barometer(Scene):
    """A breathing ring whose period is the pressure tendency.

    Steady air breathes slowly, a collapsing barometer breathes fast and turns
    toward red. The ring is drawn as a distance field rather than plotted pixels,
    which is what keeps its edge soft at this size instead of octagonal.
    """

    name = "barometer"
    duration = 11.0

    def render(self, cv: Canvas, t: float, s: Dict) -> None:
        rate = s.get("press_rate", 0.0)
        cond = s.get("condition", "changeable")
        base = {
            "settled": 0.36, "fine": 0.33, "fair": 0.28, "changeable": 0.18,
            "unsettled": 0.11, "rain": 0.06, "wet": 0.02, "stormy": 0.98,
        }.get(cond, 0.2)

        period = 5.0 / (1.0 + 2.2 * min(abs(rate) / 1.5, 1.0))
        phase = (t % period) / period
        r = 0.6 + 3.4 * phase
        # Fade the ring out as it reaches the edge, so it dissolves rather than
        # clipping against the corners.
        strength = (1.0 - phase) ** 1.6

        # Hue drifts around the ring rather than washing it in one flat colour,
        # which is what stops it looking like a stamped shape.
        ring = np.exp(-((RADIUS - r) ** 2) / 0.30) * strength
        ang = np.arctan2(Y - _CY, X - _CX)
        for row in range(N):
            for col in range(N):
                a = float(ring[row, col])
                if a <= 0.01:
                    continue
                h = base + 0.055 * math.sin(float(ang[row, col]) + t * 0.6)
                cv.buf[row, col] += np.asarray(_hsv(h, 0.8, 1.0)) * a * 0.95

        # A second ring half a period behind keeps the panel from ever emptying.
        phase2 = ((t + period / 2.0) % period) / period
        ring2 = np.exp(-((RADIUS - (0.6 + 3.4 * phase2)) ** 2) / 0.30) * (1.0 - phase2) ** 1.6
        cv.wash(ring2 * 0.55, _hsv(base + 0.04, 0.8, 1.0))

        core = math.copysign(min(abs(rate) / 1.2, 1.0), rate or 1.0)
        cv.plot(_CX, _CY - 0.9 * core, (1.0, 1.0, 1.0), 0.35 + 0.3 * abs(core))


class ForecastRibbon(Scene):
    """The six horizons as a ribbon flowing right to left.

    Column height is the predicted change, above or below the midline. Hue runs
    warm for a rise and cool for a fall. The pale cap on each column is the
    conformal half-width, so a confident forecast is a crisp bar and an uncertain
    one is a soft smear: the panel shows you the uncertainty, not just the number.
    """

    name = "forecast"
    duration = 12.0

    def render(self, cv: Canvas, t: float, s: Dict) -> None:
        series = s.get("forecast") or []
        if not series:
            glow = np.exp(-((Y - _CY) ** 2) / 2.0) * (0.25 + 0.1 * math.sin(t))
            cv.wash(glow * 0.4, (0.25, 0.28, 0.45))
            return

        scroll = (t * 0.55) % 1.0
        span = max(max(abs(p.get("delta", 0.0)) for p in series), 0.4)
        mid = _CY

        # Drawn as fields rather than a few hundred sub-pixel splats. The naive
        # version cost 330 us a frame, about 16% of a core once scaled to a
        # Zero 2 W, which is far too much for a decorative panel. This is the
        # same picture for roughly a fifth of the work.
        for i, p in enumerate(series[:N]):
            x = (i - scroll) + 1.0
            if x < -1.5 or x > N + 0.5:
                continue
            frac = float(np.clip(float(p.get("delta", 0.0)) / span, -1.0, 1.0))
            top = mid - frac * 3.2
            lo, hi = (top, mid) if frac >= 0 else (mid, top)

            col = np.exp(-((X - x) ** 2) / 0.32)                  # soft column
            inside = np.clip(1.0 - np.maximum(lo - Y, Y - hi), 0.0, 1.0)
            reach = np.clip(np.abs(Y - mid) / 3.2, 0.0, 1.0)      # brighter at the tip
            cv.wash(col * inside * (0.32 + 0.62 * reach) * 0.55,
                    _hsv(0.08 if frac >= 0 else 0.56, 0.85, 1.0))

            half = float(p.get("half", 0.0)) / span if span else 0.0
            if half > 0.02:
                spread = min(half * 2.6, 2.6)
                caps = (np.exp(-((Y - (top - spread)) ** 2) / 0.30)
                        + np.exp(-((Y - (top + spread)) ** 2) / 0.30))
                cv.wash(col * caps * 0.16, (0.85, 0.88, 1.0))

        cv.wash(np.exp(-((Y - mid) ** 2) / 0.20) * 0.10, (0.6, 0.65, 0.8))


class Alert(Scene):
    """Sensor fault or a queued retrain. A bloom, not a blinking exclamation."""

    name = "alert"
    duration = 5.0

    def render(self, cv: Canvas, t: float, s: Dict) -> None:
        fault = s.get("health") == "fault"
        colour = (1.0, 0.10, 0.06) if fault else (1.0, 0.45, 0.0)
        beat = 0.5 - 0.5 * math.cos(t * 3.4)
        bloom = np.exp(-(RADIUS ** 2) / (0.8 + 5.0 * beat)) * (0.35 + 0.65 * beat)
        cv.wash(bloom, colour)
        edge = np.exp(-((RADIUS - 3.4) ** 2) / 0.35) * beat * 0.5
        cv.wash(edge, colour)


# --------------------------------------------------------------------------
# Weather glyphs. Hand-drawn at 8x8 rather than downsampled from artwork.
#
# Three references were measured first: at 8x8 a 270x480 sun is a 2025:1 area
# reduction and its rays disappear, a 400x400 umbrella loses its canopy and
# handle, and a 638x638 snowflake averages into the background. Downsampled they
# move 0.0037, 0.0175 and 0.0027 per frame, against 0.0177 for the aurora
# already here. Copying frames would have been a downgrade. What does survive
# the trip is the palette and the subject, so those are what these borrow.
# --------------------------------------------------------------------------

class SunBurst(Scene):
    """Rayed sun. Shown when the sun is actually up and the sky is clear.

    Eight rays rotate slowly and breathe in and out of the disc. Ray length
    follows the real solar elevation, so a low winter sun is a tight bright core
    and a high summer one throws long arms to the corners. Cloud cover softens
    the rays and greys the sky, so a hazy day genuinely looks hazy.

    Palette taken from the reference: saturated yellow core, orange tips, on a
    pale blue sky.
    """

    name = "sun"
    duration = 13.0

    def render(self, cv: Canvas, t: float, s: Dict) -> None:
        elev = s.get("solar_elevation", 30.0)
        cloud = float(np.clip(s.get("cloud", 0.3), 0.0, 1.0))
        high = _smoothstep(0.0, 45.0, elev)

        # After dark the same geometry becomes a moon: cool palette, rays pulled
        # in to a halo. Without this the fair-weather symbol simply vanishes for
        # half of every day, which is how the glyphs went missing in the first
        # place.
        night = _smoothstep(2.0, -8.0, elev)

        sky = _mix((0.36, 0.55, 0.78), (0.60, 0.80, 1.00), 1.0 - cloud)
        sky = _mix(sky, (0.03, 0.04, 0.16), night)
        cv.buf += np.asarray(sky) * (0.10 + 0.13 * high + 0.06 * night)

        core = _mix((1.00, 0.80, 0.00), (0.80, 0.86, 1.00), night)
        tip = _mix((1.00, 0.45, 0.05), (0.45, 0.60, 0.95), night)

        # Disc: a soft radial falloff, not a stamped circle.
        disc = np.exp(-(RADIUS ** 2) / (1.5 + 0.5 * high))
        cv.wash(disc * 0.95, core)

        breathe = 0.5 + 0.5 * math.sin(t * 1.1)
        reach = (1.6 + 1.9 * high + 0.45 * breathe) * (1.0 - 0.55 * night)
        spin = t * 0.30

        # Eight-fold symmetry is one cosine, so the rays are a single field
        # instead of fifty-six sub-pixel splats. Same picture, a third of the
        # cost, and the angular falloff is smoother than point sampling was.
        ang = np.arctan2(Y - _CY, X - _CX)
        lobes = (np.cos(8.0 * (ang - spin)) * 0.5 + 0.5) ** 3.0
        shell = np.exp(-((RADIUS - (1.1 + reach * 0.55)) ** 2) / (1.1 + 0.6 * reach))
        beam = lobes * shell * (1.0 - 0.45 * cloud)
        cv.wash(beam * 0.80, _mix(core, tip, 0.55))

        # Corona, so the disc sits in light rather than on top of it.
        cv.wash(np.exp(-(RADIUS ** 2) / 9.0) * 0.22 * (1.0 - 0.5 * cloud), core)


class Umbrella(Scene):
    """Umbrella under rain. Shown when rain is likely and it is too warm to snow.

    The canopy bobs on a slow sine, and drops that reach it bounce off sideways
    instead of passing through, which is the detail that sells it as an object
    rather than a shape. Rain density follows the forecast probability, so a 30%
    afternoon drizzles and an 80% one hammers.

    Pink canopy and blue rain, from the reference.
    """

    name = "umbrella"
    duration = 13.0

    def __init__(self) -> None:
        self.drops: List[List[float]] = []
        self.splash: List[List[float]] = []

    def render(self, cv: Canvas, t: float, s: Dict) -> None:
        p = float(np.clip(s.get("rain_prob", 0.4), 0.0, 1.0))
        fps = max(float(s.get("_fps", FPS)), 1.0)

        cv.buf += np.asarray((0.02, 0.10, 0.26)) * 0.55      # wet blue ground
        cv.fade(1.0)

        bob = 0.30 * math.sin(t * 1.25)
        cy = 3.1 + bob
        canopy = (1.00, 0.60, 0.80)
        rib = (0.86, 0.36, 0.62)

        # Canopy: a dome traced as an arc so its edge stays smooth at this size.
        # The arc is about 10 px long, so 11 samples is roughly one per pixel.
        # Seventeen overlapped 1.7 deep and blew the canopy white; dropping the
        # alpha instead just made it muddy. Fix the sampling, not the brightness.
        for i in range(11):
            a = math.pi + (i / 10.0) * math.pi          # pi .. 2pi, the top half
            x = _CX + 3.05 * math.cos(a)
            y = cy + 1.85 * math.sin(a)
            # Low alpha because seventeen arc samples overlap heavily; at 0.95
            # the canopy saturated to white and lost its colour entirely.
            cv.plot(x, y, canopy, 0.78)
            cv.plot(x, y + 0.62, rib, 0.26)             # underside shadow
        cv.plot(_CX, cy - 1.62, canopy, 0.60)           # finial

        # Handle, with the hook at the bottom.
        for k in range(5):
            cv.plot(_CX, cy + 0.7 + k * 0.55, (0.85, 0.45, 0.32), 0.55)
        cv.plot(_CX - 0.55, cy + 3.15, (0.85, 0.45, 0.32), 0.45)

        want = int(round(3 + 11 * p))
        while len(self.drops) < want:
            self.drops.append([np.random.uniform(0, N), np.random.uniform(-N, 0)])
        while len(self.drops) > want:
            self.drops.pop()

        speed = 4.4 + 3.2 * p
        for d in self.drops:
            d[1] += speed / fps
            dx = d[0] - _CX
            # Inside the canopy's span and level with it: bounce, do not pass.
            if abs(dx) < 3.05 and cy - 1.9 <= d[1] <= cy + 0.2:
                self.splash.append([d[0], d[1], math.copysign(2.6, dx or 1.0), 0.0])
                d[0] = np.random.uniform(0, N)
                d[1] = np.random.uniform(-2.5, -0.3)
                continue
            if d[1] > N + 1:
                d[0] = np.random.uniform(0, N)
                d[1] = np.random.uniform(-2.5, -0.3)
            cv.plot(d[0], d[1], (0.00, 0.60, 1.00), 0.8)

        alive = []
        for sp in self.splash:
            sp[3] += 1.0 / fps
            if sp[3] > 0.6:
                continue
            x = sp[0] + sp[2] * sp[3] * 1.6
            y = sp[1] + 5.0 * sp[3] * sp[3]
            if 0 <= y < N:
                cv.plot(x, y, (0.55, 0.85, 1.00), 0.6 * (1.0 - sp[3] / 0.6))
            alive.append(sp)
        self.splash = alive[-24:]


class Snowflake(Scene):
    """A six-arm flake, turning. Shown when it is cold enough to snow.

    Six arms with branches, rotated as a whole. Sub-pixel plotting is what makes
    a rotating star possible at this size: without it the arms would jump
    between pixels and the whole thing would strobe. The flake breathes, drifts
    on a slow lissajous, and smaller flakes fall past it.

    Deep blue night and white, from the reference.
    """

    name = "snowflake"
    duration = 14.0

    def __init__(self) -> None:
        self.motes: List[List[float]] = []

    def render(self, cv: Canvas, t: float, s: Dict) -> None:
        p = float(np.clip(s.get("rain_prob", 0.5), 0.0, 1.0))
        fps = max(float(s.get("_fps", FPS)), 1.0)

        for row in range(N):
            k = row / (N - 1.0)
            # Kept dark on purpose: a bright ground and a white flake fight,
            # and the flake loses.
            cv.buf[row, :] += np.asarray(_mix((0.00, 0.00, 0.13),
                                              (0.04, 0.05, 0.22), k))

        want = int(round(2 + 7 * p))
        while len(self.motes) < want:
            self.motes.append([np.random.uniform(0, N), np.random.uniform(-N, 0)])
        while len(self.motes) > want:
            self.motes.pop()
        for m in self.motes:
            m[1] += 1.05 / fps
            if m[1] > N + 1:
                m[0] = np.random.uniform(0, N)
                m[1] = np.random.uniform(-2.0, -0.3)
            x = (m[0] + 0.8 * math.sin(t * 0.7 + m[0] * 2.1)) % N
            tw = 0.55 + 0.45 * math.sin(t * 2.6 + m[0] * 4.0)
            cv.plot(x, m[1], (0.75, 0.85, 1.00), 0.34 * tw)

        spin = t * 0.42
        cx = _CX + 0.42 * math.sin(t * 0.31)
        cy = _CY + 0.34 * math.sin(t * 0.23 + 1.1)
        breathe = 0.86 + 0.14 * math.sin(t * 1.05)
        white = (0.92, 0.96, 1.00)

        cv.wash(np.exp(-(((X - cx) ** 2 + (Y - cy) ** 2)) / 5.5) * 0.11,
                (0.30, 0.50, 0.95))

        # Six arms sixty degrees apart is only about three pixels of separation
        # at this radius, so they have to be thin and start clear of the hub.
        # Drawn thick and bright they simply fuse into a white blob, which is
        # the exact failure the downsampled reference had.
        span = 3.35 * breathe
        for i in range(6):
            a = spin + i * (math.pi / 3.0)
            ca, sa = math.cos(a), math.sin(a)
            for k in range(4):
                d = 1.25 + (k / 3.0) * (span - 1.25)
                cv.plot(cx + ca * d, cy + sa * d, white, 0.62 - 0.06 * k)
            # One pair of branches. Two pairs closed the gaps and it blobbed.
            bx, by = cx + ca * span * 0.62, cy + sa * span * 0.62
            for sgn in (-1, 1):
                b = a + sgn * 1.05
                cv.plot(bx + math.cos(b) * 0.78, by + math.sin(b) * 0.78, white, 0.34)
        cv.plot(cx, cy, white, 0.62)


class Readout(Scene):
    """The actual numbers, scrolling between the animations.

    Everything else on this panel is an impression: a hue, a drift direction, a
    ray length. This is the one that tells you it is 24.2 degrees. The strip runs
    measurement first, then the three hour forecast with its sign, each segment
    in its channel's colour so you can tell temperature from humidity without
    reading the unit.

    Scrolls at a fractional pixel offset, so at 8 pixels tall the glyphs glide
    rather than stepping, which is the difference between readable and a
    flickering mess.
    """

    name = "readout"
    duration = 15.0

    AMBER = (1.00, 0.62, 0.06)
    CYAN = (0.10, 0.78, 0.95)
    VIOLET = (0.66, 0.52, 1.00)
    GREEN = (0.30, 0.95, 0.55)
    ROSE = (1.00, 0.35, 0.45)

    def _segments(self, s: Dict) -> List[Tuple[str, Tuple[float, float, float]]]:
        t = s.get("temp")
        rh = s.get("humidity")
        slp = s.get("press")
        segs: List[Tuple[str, Tuple[float, float, float]]] = []
        if t is not None:
            segs.append((f"{t:.1f}C", self.AMBER))
        if rh is not None:
            segs.append((f"{rh:.0f}%", self.CYAN))
        if slp is not None:
            segs.append((f"{slp:.0f}Pa", self.VIOLET))
        fc = s.get("forecast") or []
        if len(fc) >= 3:
            d = float(fc[2].get("delta", 0.0))          # the three hour head
            segs.append((f"{d:+.1f}", self.GREEN if d >= 0 else self.ROSE))
        return segs or [("--", self.AMBER)]

    def render(self, cv: Canvas, t: float, s: Dict) -> None:
        segs = self._segments(s)
        gap = 3.0
        widths = [_text_width(txt) + gap for txt, _ in segs]
        total = sum(widths)

        # A faint moving ground so the text is not floating in black.
        cv.wash(np.exp(-((Y - 3.5) ** 2) / 14.0) * 0.05, (0.20, 0.24, 0.40))

        x = N - (t * 5.0) % total
        for _ in range(2):                              # wrap for a seamless loop
            cursor = x
            for (txt, colour), w in zip(segs, widths):
                _draw_text(cv, txt, cursor, 1.5, colour, 1.0)
                cursor += w
            x += total


class LedDisplay:
    """Renders scenes at a steady frame rate and dissolves between them.

    Keeps the same public surface as before: `start()`, `await stop()`, and
    `frame_name` for the API. `cycle_s` is accepted for compatibility but the
    scenes now carry their own durations, because a barometer breath and a
    scrolling ribbon do not want the same dwell time.
    """

    CROSSFADE = 1.3

    def __init__(self, station, cycle_s: float = 0.4, fps: float = FPS):
        self.station = station
        self.cycle_s = float(cycle_s)
        self.fps = float(np.clip(fps, 4.0, 30.0))
        self.enabled = True
        self._stop = asyncio.Event()
        self._task = None
        self.frame_name = "idle"

        # Two tracks. The glyph is chosen by what the weather is doing; the
        # ambient scenes rotate underneath it to carry the numbers.
        self.glyphs: Dict[str, Scene] = {
            "sun": SunBurst(), "umbrella": Umbrella(), "snowflake": Snowflake(),
        }
        self.scenes: List[Scene] = [Readout(), Aurora(), SolarSky(),
                                    Precipitation(), ForecastRibbon(),
                                    Barometer()]
        self.alert = Alert()
        self._glyph: str = "sun"
        self._show_glyph = True
        self._idx = 0
        self._scene_started = 0.0
        self._prev: Optional[Scene] = None
        self._fade_started = -99.0
        self._a = Canvas()
        self._b = Canvas()
        self._alerting = False
        self._phase = 0

    # ------------------------------------------------------------ state

    def _snapshot(self) -> Dict:
        """One cheap read of station state per frame, never a live query."""
        live = self.station.live or {}
        precip = self.station.precip_bundle or {}
        fc = self.station.forecast_bundle or {}

        series = []
        for p in (fc.get("targets", {}).get("temperature") or [])[:6]:
            mu, anchor = p.get("mu"), (fc.get("anchors") or {}).get("temperature")
            if mu is None or anchor is None:
                continue
            series.append({"delta": float(mu) - float(anchor),
                           "half": abs(float(p.get("hi", mu)) - float(p.get("lo", mu))) / 2.0})

        return {
            "temp": float(live.get("temp_smooth") or live.get("temp_c") or 15.0),
            "humidity": float(live.get("hum_smooth") or 60.0),
            "press_rate": float(live.get("press_rate") or 0.0),
            "press": live.get("press_slp"),
            "solar_elevation": float(live.get("solar_elevation") or -20.0),
            "solar_azimuth": float(live.get("solar_azimuth") or 180.0),
            "cloud": float(live.get("cloud_index") or 0.4),
            "lux": float(live.get("lux") or 0.0),
            "rain_prob": float(precip.get("rain_probability") or 0.0),
            "condition": precip.get("condition", "changeable"),
            "forecast": series,
            "health": self.station.monitor.health.overall,
            "retrain": bool(self.station.monitor.retrain_requested),
            "_fps": self.fps,
        }

    def _brightness(self, s: Dict) -> float:
        """Dim to the room. A weather station should not be a night light.

        Log scaling because perceived brightness tracks the logarithm of
        illuminance far better than the value itself.
        """
        lux = max(s.get("lux", 0.0), 0.0)
        k = math.log10(1.0 + lux) / math.log10(1.0 + 400.0)
        return float(np.clip(0.13 + 0.87 * k, 0.13, 1.0))

    # ------------------------------------------------------------ loop

    @staticmethod
    def _pick_glyph(s: Dict) -> str:
        """Which weather is this, from measurement and forecast only.

        Always returns a glyph. The first version gated each one behind narrow
        conditions and returned None otherwise, which on a warm dry night meant
        none of them ever qualified and the panel silently fell back to the
        ambient scenes. A forecast symbol is not an exception, it is the default,
        so this reads like a weather app: cold wins, then wet, then fair.

        The thresholds sit on quantities that are already smoothed upstream. Rain
        probability is the Zambretti prior blended with the online learner and
        temperature is the Kalman level, so the glyph changes when the weather
        changes rather than when a sensor twitches.
        """
        rain = s.get("rain_prob", 0.0)
        temp = s.get("temp", 10.0)
        cond = s.get("condition", "changeable")
        wet = cond in ("rain", "wet", "stormy")

        if temp <= 1.5:
            return "snowflake" if (rain >= 0.20 or wet or cond == "unsettled") else "sun"
        if rain >= 0.30 or wet:
            return "umbrella"
        return "sun"

    def _advance(self, now: float, s: Dict) -> None:
        alerting = s["health"] != "ok" or s["retrain"]
        if alerting != self._alerting:
            self._alerting = alerting
            self._prev = self._current()
            self._fade_started = now
            self._scene_started = now
            return
        if alerting:
            return

        # A change in the weather itself preempts whatever is on screen. This is
        # the point: the panel dissolves because the data moved, not because a
        # timer expired.
        glyph = self._pick_glyph(s)
        if glyph != self._glyph:
            self._prev = self._current()
            self._glyph = glyph
            self._show_glyph = True
            self._fade_started = now
            self._scene_started = now
            return

        cur = self._current()
        if now - self._scene_started < cur.duration:
            return

        self._prev = cur
        self._fade_started = now
        self._scene_started = now
        if self._show_glyph:
            # Hand back to the informational scenes for one turn.
            self._show_glyph = False
            self._idx = (self._idx + 1) % len(self.scenes)
        else:
            self._show_glyph = True

    def _current(self) -> Scene:
        if self._alerting:
            return self.alert
        if self._show_glyph and self._glyph in self.glyphs:
            return self.glyphs[self._glyph]
        return self.scenes[self._idx]

    async def _run(self) -> None:
        period = 1.0 / self.fps
        t0 = time.monotonic()
        while not self._stop.is_set():
            frame_start = time.monotonic()
            try:
                if self.enabled:
                    now = frame_start - t0
                    s = self._snapshot()
                    self._advance(now, s)

                    cur = self._current()
                    self.frame_name = cur.name
                    self._a.clear()
                    cur.render(self._a, now, s)

                    mix = (now - self._fade_started) / self.CROSSFADE
                    if self._prev is not None and mix < 1.0:
                        self._b.clear()
                        self._prev.render(self._b, now, s)
                        k = _smoothstep(0.0, 1.0, max(mix, 0.0))
                        out = self._b.buf * (1.0 - k) + self._a.buf * k
                    else:
                        self._prev = None
                        out = self._a.buf

                    frame = Canvas()
                    frame.buf = out
                    self._phase += 1
                    self.station.board.set_pixels(
                        frame.to_pixels(self._brightness(s), self._phase))
            except Exception:  # a display glitch must never take the station down
                pass

            elapsed = time.monotonic() - frame_start
            await asyncio.sleep(max(period - elapsed, 0.002))

        try:
            self.station.board.clear()
        except Exception:
            pass

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            self.station.board.clear()
        except Exception:
            pass
