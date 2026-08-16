# Design notes

Internals, tuning and failure modes. This document deliberately does **not**
repeat the README: no pitch, no install instructions, no feature list, no API
table. It covers what you need when changing the code or debugging a station
that is behaving oddly, and nothing else.

Read this before touching anything under `ashvale/models/`.

---

## 1. The data model

### Schema

One wide table, `telemetry`, keyed on a float epoch `ts`. Every column is
`REAL` except `tier`. There is no normalisation, because a sensor sample is a
single denormalised event and joins on a Pi are not free.

| Column | Meaning |
|---|---|
| `temp_raw` | Straight off the HTS221/LPS25HB average, uncompensated |
| `temp_c` | After self-heating compensation, before filtering |
| `temp_smooth`, `temp_rate` | Kalman level and rate (°C, °C/h) |
| `hum`, `hum_smooth` | Raw and filtered relative humidity |
| `press`, `press_slp` | Station pressure and its sea-level reduction |
| `press_smooth`, `press_rate` | Kalman level and rate (hPa, hPa/h) |
| `cpu_temp` | SoC thermal zone. The nuisance variable |
| `dew_c` | Magnus dew point from smoothed inputs |
| `lux`, `r`, `g`, `b` | TCS3400 clear and colour channels |
| `pitch` … `gz` | IMU, nine values |
| `tier` | 0 raw, 1 five-minute mean, 2 hourly mean |

Supporting tables: `forecasts` (issued, valid, target, mu, lo, hi), `labels`
(human ground truth), `scores` (verification output), `events` (station log).

**Three columns are named `r`, `g` and `b`.** This has already caused one bug:
`GROUP BY b` in an aggregation query silently grouped by the blue channel,
because SQLite resolves an unqualified identifier to a real column in
preference to a result alias. It returned one row per sample while cheerfully
reporting the requested bucket size. Never use a single-letter alias in this
schema.

### Tiering

Nothing is deleted, only downsampled. `Store.compact()` folds raw rows older
than `raw_retention_days` into five-minute means, and five-minute rows older
than `five_min_retention_days` into hourly means. A year of history lands
around 30 MB.

The `tier` column exists so you can tell a genuine hourly observation from a
mean of twelve. Range queries deliberately mix tiers, which is right for
display but means **a query spanning the raw/five-minute boundary has
non-uniform effective resolution**. If you ever compute a statistic that
assumes equal weight per row, weight by tier or restrict to one.

### Why aggregation happens in SQL

`range_series()` buckets with `CAST(ts/bucket AS INTEGER)*bucket` and
`AVG`/`MIN`/`MAX` inside SQLite. Pulling ninety days of rows into numpy to
average them costs more resident memory than the board has. The bucket is
chosen from the span and snapped to a ladder of familiar durations, targeting
about 700 points, because neither a browser nor a human benefits from more.

Min and max travel alongside the mean so the UI can shade a true range band. A
two-hour bucket that spanned four degrees should not render as a flat line.

---

## 2. The estimation layer

### Self-heating compensation

Model: `T = T_raw − k·(T_cpu − T_raw)`, with `k ≥ 0` estimated by recursive
least squares against any trusted reference you supply.

The regressor is `φ = max(T_cpu − T_raw, 0)` and the target is
`T_raw − T_ref`, so `k·φ` should equal the observed bias. One step, forgetting
factor 0.98:

```
gain = P·φ / (λ + φ·P·φ)
k   ← clip(k + gain·(target − k·φ), k_min, k_max)
P   ← (P − gain·φ·P) / λ
```

Measured behaviour: recovers a true `k = 0.62` from a prior of `0.30` in a
**single sample**, and holds post-calibration bias to 0.012 °C over 200
subsequent readings.

**The clamp is not decoration.** A single mistyped reference drives `k` to its
bound, and because state persists across restarts it stays there, quietly
biasing every reading until you notice. `POST /api/calibrate {"reset": true}`
exists for exactly that.

**If you write a simulator or a test fixture, the forward model must be the
exact inverse:** `T_raw = (T + k·T_cpu)/(1 + k)`. Generating the bias as
`T + k·(T_cpu − T)` is a different relation, and the mismatch injects roughly
1.2 °C of phantom noise floor that caps every skill score. This has already
happened once.

### Humidity compensation

`HumidityCompensator` carries an additive `offset` on relative humidity,
estimated from a trusted hygrometer by the same one-step RLS used for `k`, with
the regressor fixed at 1 so repeated calibrations converge to a weighted mean.
Clamped to +/-35% for the same reason `k` is clamped.

It also implements a psychrometric term, moving RH from the element's
temperature onto the compensated air temperature through conserved vapour
pressure, `RH_true = RH_sensor * es(T_sensor) / es(T_true)`. That term is
**off by default**, and the reason is worth recording. The thermal argument
predicts a hot element reads LOW. Measured against a reference hygrometer this
board read 75.4% where the truth was 50.4%, so it reads HIGH by 25 points, and
the correction would have pushed it further the wrong way. The dominant error on
this hardware is additive element bias, not a thermal gradient.

If you enable `sensor.hum_psychrometric`, `scripts/simulate.py` applies the exact
inverse when generating synthetic humidity. It has to: the same
simulator/compensator algebra trap described above for temperature applies here,
and getting it wrong bakes in a bias no calibration can remove.

### The Kalman bank

One constant-velocity filter per signal. State `x = [level, rate]`, standard
continuous white-noise-acceleration process model:

```
F = [[1, Δt], [0, 1]]
Q = q · [[Δt³/3, Δt²/2], [Δt²/2, Δt]]
H = [1, 0]
```

Two implementation points that matter.

**Joseph form.** The update is `P ← (I−KH)·P·(I−KH)ᵀ + K·R·Kᵀ`, not the
shorter `P ← (I−KH)·P`. The short form accumulates asymmetry and loses positive
semi-definiteness over months of continuous running, and nobody notices until
the filter quietly stops working.

**`q` is tuned for the live 2 s cadence.** Because `Q` scales with `Δt³`,
running the same filter at a 300 s step makes the process noise five orders of
magnitude larger, at which point the filter abandons smoothing and tracks
measurement noise. `scripts/simulate.py` therefore scales `q` by
`(sample_period/step)³` when backfilling. Before that fix, rate estimates blew
past anything physical and enshrined a −37 hPa/h all-time record.

`nis` (normalised innovation squared) is exposed per filter. It should hover
near 1. Persistently high means the filter is too confident and lagging real
change; persistently low means you are over-smoothing.

---

## 3. Features

33 columns, defined once in `FEATURE_NAMES`. An assertion in
`build_features()` fails loudly if the stacked matrix width drifts from that
list, which is the cheapest guard available against a silently misaligned
design matrix.

Three rules govern what goes in.

1. **Anything closed-form is computed, not learned.** Dew point, wet bulb,
   VPD, absolute humidity, solar elevation and the clear-sky cloud index come
   from `physics.py`. Making a linear learner rediscover the Magnus curve from
   data wastes both samples and capacity.
2. **Anything periodic is a sine/cosine pair.** Two diurnal harmonics and one
   annual, so phase is representable without a discontinuity at midnight.
3. **Lags are expressed in hours, not samples.** `press_tend_3h` means three
   hours whatever `grid_s` is. Changing the grid must not silently change what
   the model means by "three hours ago".

### Standardisation

`Standardiser` keeps streaming Welford moments and z-scores everything except
the bias column. Not optional: unscaled pressure sits near 1013 while unscaled
temperature rate sits near 0.02, and the resulting condition number will
embarrass you.

### Solar geometry as a feature

`solar_position()` is the NOAA low-precision model, accurate to a few tenths of
a degree and costing about twenty floating point operations. It gives the
diurnal cycle real physical structure rather than making the model infer it
from clock time alone. `clear_sky_irradiance()` turns measured lux into a crude
cloudiness index by ratio. Through a south-facing window that is a surprisingly
decent okta estimate; in a north-facing room it is nearly useless. Judge
accordingly.

---

## 4. The forecasting core

### Why RLS and not gradient descent

A station produces 288 grid rows a day. Sample efficiency dominates everything
else. RLS is the exact minimiser of the exponentially weighted squared error at
every step, not an approximation, so it converges in far fewer samples than
SGD. The covariance `P` is a genuine parameter-uncertainty estimate, free. One
`(33, 33)` matrix is about 8 kB, so the whole bank of 18 fits in L2 cache on a
Cortex-A53.

### The covariance trace cap

```
P ← (P − outer(gain, P·x)) / λ
P ← (P + Pᵀ)/2                    # enforce symmetry
if trace(P) > p_max: P *= p_max/trace(P)
```

**This is the single most important guard in the file.** Plain exponential
forgetting inflates `P` without bound along directions the data never excites.
On a quiet night the regressor barely moves, `P` grows exponentially in the
unexcited subspace, and the model detonates on the first sunrise sample. It is
the most common way a field RLS deployment dies. If you refactor `rls.py`, keep
both the cap and the symmetrisation.

### Direct heads, not recursion

18 heads: 3 targets × 6 horizons. Each predicts a **delta from now**, and the
absolute forecast is reconstructed as `anchor + delta`.

Predicting deltas rather than levels matters more than it looks. A model that
must output 14.7 °C spends its capacity representing the mean; one that outputs
+0.4 °C spends it on the weather.

Iterating a single one-step model 288 times to reach 24 hours would compound
its own bias into a beautifully smooth lie. 18 direct heads cost about 150 kB
total and each is honest about its own horizon.

### The Hedge ensemble

Each head blends three members: persistence (delta = 0), climatology (delta
from the harmonic fit) and the learned RLS output. Weights update by
exponentiated gradient on normalised absolute loss, then renormalise. This
guarantees the ensemble is never much worse than its best member, and it
re-weights within about a day when the season turns.

**The ensemble is allowed to conclude the learned model is useless.** At
15-minute pressure it typically parks most of its weight on persistence. That
is correct behaviour surfaced honestly, not a defect to engineer away.

### The thermostat member

A room held at a setpoint is not the same process as a room that is free to
drift. It is a closed loop, and persistence, the baseline everything here is
scored against, is simply the wrong statement about it: the truth is not "it
stays where it is", it is "it returns to the setpoint".

So when `site.heating` is on, the ensemble gains a fourth member:

```
dT_set(h) = (T_set - T_now) * (1 - exp(-h / tau))
```

First order, because that is what a controlled system is: `tau` is the time to
close about 63% of the gap. Zero at h = 0, asymptotic to the full correction.

Humidity follows and is the part that is easy to get wrong. Heating adds no
moisture, so what is conserved is vapour pressure, not relative humidity:

```
RH(h) = RH_now * es(T_now) / es(T_now + dT_set(h))
```

Warm the air and RH falls although nothing was dried. This is why a heated house
in winter is dry, and the test asserts the dew point is unchanged to 1e-6.

Pressure gets zero: a thermostat cannot move the synoptic field.

**It is offered, not imposed.** The Hedge weights score this member against the
others on realised error like any other, so a wrong `tau` or a setpoint you
forgot to update costs accuracy and gets down-weighted, rather than quietly
biasing every forecast. With heating off the member returns zero, which makes it
identical to persistence and therefore harmless.

Adding it changed the member count from three to four, so `ForecastHead.from_dict`
reinitialises `weights` **and** `member_mae` when a saved head has the old
length. Missing the second one did not fail on load: it failed later inside
`learn()` on a broadcast error, which is a much worse place to find out.

### Adaptive conformal intervals

Split conformal is valid only under exchangeability, and weather is emphatically
not exchangeable: a front arrives and yesterday's residual quantile becomes
fiction. Adaptive conformal inference (Gibbs and Candès) feeds realised coverage
back into the working α:

```
α ← clip(α + γ·(α_target − 1[y ∈ C]), 0.005, 0.75)
```

The band widens after each miss and narrows after each hit, so long-run
coverage tracks the target whatever the distribution does underneath.

**Coverage is the acceptance test for any change to this path.** Measured
coverage sits at 89 to 91% against a 90% target across all 18 heads. A change
that improves MAE while coverage drifts to 70% is a regression, not an
improvement, because the intervals have started lying.

---

## 5. Verification

`Station.verify()` runs every five minutes. It pulls forecasts whose validity
time has passed, looks up the truth and the anchor, and computes per bucket:

```
skill = 1 − MAE_model / MAE_persistence
```

Zero means no better than assuming nothing changes. **Negative is useful
information, not an embarrassment**: it says ship persistence at that horizon
and stop pretending.

Scored errors feed two places: the conformal calibrator for each head, and, for
horizons up to 3 h, the Page-Hinkley drift detector. Forecasts more than an
hour past validity are deleted so the table does not grow without bound.

A forecast that is never scored is an opinion. A forecast scored against
persistence is a measurement.

### Reproducible evidence

`scripts/simulate.py` takes `--seed` and `--end`. **Both must be pinned** for
run-to-run comparability. The seed fixes the OU realisation, but the wall-clock
anchor moves solar elevation and the seasonal harmonic, so an unpinned `--end`
changes temperature and humidity while leaving pressure bit-identical. That
asymmetry is a useful diagnostic in itself: if pressure differs between two runs
with the same seed, something other than the anchor has changed.

Note the interaction with `evaluate.py`: `store.window()` looks back from *now*
with a 60-day default, so a history pinned to a date in the past reports "not
enough history" unless you pass a wide `--hours`.

---

## 6. Monitoring

Three detectors, because they fail differently.

**`MahalanobisEWMA`** catches abrupt multivariate novelty: a window opening, a
heater cycling, a squall. The EWMA on the whitened residual gives
persistence-aware detection, so one odd sample is noise and ten in a row is an
event. Shrinkage toward a scaled identity is **not optional**: with six signals
the sample covariance is singular for the first hour, and a singular covariance
turns Mahalanobis distance into a random number generator with an authoritative
name.

**`PageHinkley`** catches slow change: a sensor drifting, a season turning, a
model going stale. It runs on forecast error and **triggers retraining**, which
is a far better signal than a cron schedule.

**`SensorHealth`** catches the quietest failure of all. A latched sensor looks
perfectly normal to both detectors above: the dashboard is fine, the model
trains happily, and every forecast is confidently wrong. Bit-identical
consecutive readings are the only tell.

---

## 7. Tuning

| Symptom | Knob | Direction |
|---|---|---|
| Temperature reads consistently high | Calibrate from the Models and Calibration tab, or `sensor.cpu_heat_k` | Raise |
| Humidity reads consistently off | Calibrate against a reference hygrometer, or `sensor.hum_offset` | Either |
| Readings over-smoothed, lag real change | `sensor.kalman_q_temp` | Raise |
| Rates look noisy | `sensor.kalman_q_*` down, or `kalman_r_*` up | |
| NIS persistently much above 1 | Filter too confident, raise `q` | Raise |
| NIS persistently much below 1 | Over-smoothing, lower `q` | Lower |
| Adapts too slowly to a season change | `model.rls_forgetting` toward 0.995 | Lower |
| Jumpy, forgets overnight | `model.rls_forgetting` toward 0.9995 | Raise |
| Coverage well below target | `model.conformal_gamma` | Raise |
| Coverage well above target, lazily wide bands | `model.conformal_gamma` | Lower |
| Drift alarms constantly | `model.drift_lambda` | Raise |
| Retrains eat the CPU | `model.train_period_s` up, `max_pairs` down | |
| Zambretti reads pessimistic everywhere | `site.altitude_m` is wrong | Fix it |

On forgetting factors: effective memory is `1/(1−λ)` samples. At `λ = 0.9985`
on a five-minute grid that is about 667 samples, roughly 55 hours. `λ = 0.99`
is about two hours and will chase noise.

---

## 8. Performance budget on a Zero 2 W

Roughly 20× slower than a modern x86 core. Measured:

| Operation | x86 | Zero 2 W |
|---|---|---|
| Backfill 4032 rows | 0.6 s | 11.8 s |
| Full retrain, 18 heads | ~5 s | 60 to 100 s |
| Resident set, steady state | | ~150 MB |
| Database, one year | | ~30 MB |

Retraining runs in a worker thread via `asyncio.to_thread`, so the sample loop,
the API and the LED never stall. A command that looks hung on the Pi is usually
just the Pi.

`max_pairs` in `NowcastEnsemble.fit` caps supervised pairs per head at the most
recent 2500. Not a shortcut: with `λ = 0.9985` the 4000th-most-recent sample
carries a weight of about `e⁻⁶`. It costs real seconds on a Cortex-A53 and buys
nothing measurable.

**One uvicorn worker, deliberately.** The station owns mutable model state; a
second worker would give you two divergent forecasters sharing a socket.

---

## 9. Extension points

**Adding a target.** Append to `model.targets` in config and ensure
`station.py` resamples it into the `series` dict; the head bank scales
automatically. Add a climatology column too, or the ensemble's climatology
member returns zero delta forever and quietly wastes a third of its weight.

**Adding a feature.** Append to `FEATURE_NAMES` **and** to the `column_stack`
in `build_features()`, in the same position. The width assertion catches
mismatches. Persisted RLS state from before the change is now the wrong
dimension: delete `data/state/station_state.json` and retrain.

**Adding a sensor.** `sensors.py` is the only module that touches hardware.
Extend `SenseBoard.read()`, add the column to `storage.COLUMNS` and the schema,
and mirror it in `SimulatedBoard` so the simulator still exercises every path.
A DS18B20 outside the window is the highest-value hardware change available: it
removes the indoor caveat entirely and improves every model at once.

**Replacing the learner.** `ForecastHead` needs only `predict(x)` and
`update(x, y)`. Anything with that interface drops in. If you swap in something
without a covariance you lose `predict_std`, and the conformal fallback `sigma`
becomes meaningless until enough residuals accumulate.

---

## 10. Things that will bite you

- **Station pressure passed where sea-level pressure is expected.** Silent, and
  at 100 m elevation it shifts the Zambretti number by about two categories,
  permanently.
- **Single-letter SQL aliases.** See section 1.
- **`$` as a JavaScript identifier** in `dashboard.py`. It collides with
  bundled libraries and kills the entire script with one opaque syntax error.
  The helper is `el()`.
- **Unconstrained Chart.js canvases.** With `maintainAspectRatio: false` a
  chart expands to fill its parent. Every canvas needs an explicitly sized
  relative wrapper or it swallows its panel.
- **Charts inside `display: none`.** Chart.js cannot measure a hidden canvas,
  so tab switches call `resize()` on reveal.
- **Persisted state outliving a schema change.** `station_state.json` holds
  trained parameters with fixed dimensions. Change the feature count without
  deleting it and you get a shape error at the first update, or worse, silence.
- **Synthetic history left in the database.** After a `simulate.py` backfill,
  clear the synthetic rows once real telemetry accumulates, or the model stays
  anchored on a stochastic weather model rather than on your room.
