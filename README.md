# Ashvale Station

An online machine learning suite for a Raspberry Pi Zero 2 W with a Sense HAT v2.
It turns the original telemetry dashboard into a forecasting instrument: multi-horizon
predictions with calibrated uncertainty, a verification scorecard that scores the model
against persistence, drift detection that triggers its own retraining, and a
human-in-the-loop labelling path.

Everything runs on the Pi. No cloud, no GPU, no PyTorch, no scikit-learn, no pandas.
The learners are pure numpy and the whole process sits comfortably under 150 MB RSS.

```
python scripts/simulate.py --days 14 --wipe   # seed synthetic history
python scripts/evaluate.py                    # walk-forward backtest
python run.py                                 # serve on :8000
```

---

## Why the design looks like this

A single point sensor on a windowsill is not a weather service, and pretending
otherwise is the fastest way to build something that looks impressive and is
useless. The honest inventory of what your hardware can actually observe:

| Signal | What it tells you | Useful range |
| --- | --- | --- |
| Pressure and its tendency | Synoptic systems, and it passes through walls | Genuinely hours ahead |
| Temperature, humidity | The local micro-environment | Hours, strongly diurnal |
| Ambient light and colour | Cloudiness, occupancy, time of day | Now |
| IMU | Whether someone knocked the desk | Now |

So the suite is built around that reality. Short horizons lean on state estimation and
learned dynamics. Long horizons lean on climatology plus a decaying anomaly, and are
labelled an *outlook* rather than a forecast. Every claim gets scored against the
"nothing changes" baseline, in public, on the dashboard.

### The stack

```
sensors.py       hardware + a physics-based simulator fallback
    |
estimation.py    self-heating compensation -> Kalman bank -> level + rate
    |
storage.py       SQLite, WAL, tiered downsampling (raw -> 5 min -> hourly)
    |
features.py      33 features on a 5-minute grid, physics computed not learned
    |
models/
  rls.py         recursive least squares + adaptive conformal intervals
  nowcast.py     18 direct heads (3 targets x 6 horizons), Hedge-blended
  climatology.py harmonic regression for the 7-day outlook
  precip.py      Zambretti prior + online logistic residual learner
  anomaly.py     Mahalanobis EWMA + Page-Hinkley drift + sensor health
    |
station.py       four async loops: sample / persist / train / verify
api.py, led.py, dashboard.py
```

### Six decisions worth defending

**1. Self-heating is a grey-box parameter, not a magic constant.**
The HTS221 and LPS25HB sit millimetres above a SoC running 20 to 25 °C hotter than the
room. The usual fix is `T = T_sensor - (T_cpu - T_sensor) / 1.5`. That 1.5 depends on
your case, your orientation, your airflow, and your CPU load. Here it is a single RLS
parameter that you update from the dashboard by typing in a thermometer reading. In
testing it recovers a known coefficient of 0.62 from a prior of 0.30 in **one sample**,
and holds post-calibration bias to 0.012 °C.

**2. Rates come from a Kalman filter, never a finite difference.**
Pressure tendency is the single most informative variable you have, and the LPS25HB
noise floor makes a naive `(p[t] - p[t-1])/dt` pure noise. A constant-velocity Kalman
filter estimates level and rate jointly, in Joseph form so the covariance stays positive
semi-definite over months of continuous operation. The filtered `dp/dt` is what feeds
both Zambretti and the learned heads.

**3. Direct multi-horizon heads, not one model iterated forward.**
Iterating a one-step model 288 times to reach 24 hours compounds its own bias into a
beautifully smooth lie. Eighteen small direct heads cost about 150 kB total and each one
is honest about its own horizon.

**4. RLS with directional forgetting, not SGD.**
A station produces 288 grid rows a day. Sample efficiency is not a nicety. RLS is the
exact minimiser of the exponentially weighted squared error at every step and converges
in far fewer samples. The covariance `P` gives free parameter uncertainty. The forgetting
factor (0.9985, about 11 hours of effective memory) handles seasonal adaptation without
any retraining schedule at all. Plain forgetting inflates `P` exponentially during quiet
nights when the regressor barely moves, so the trace is capped: this is the single most
common way a field RLS deployment detonates.

**5. Adaptive conformal intervals, not Gaussian error bars.**
Split conformal assumes exchangeability. Weather is not exchangeable: a front arrives and
yesterday's residual quantile becomes fiction. Adaptive conformal inference feeds realised
coverage back into the working alpha, so the band widens after each miss and narrows after
each hit. Measured coverage in the backtest below sits at 89 to 91% against a 90% target,
across every target and horizon.

**6. The ensemble is allowed to conclude that the model is useless.**
Each head blends persistence, climatology and the learned model with Hedge weights.
At 15-minute pressure the weights land on **96% persistence**, which is the correct
answer, and the scorecard says so out loud. A forecasting system that cannot tell you
when to switch it off is a marketing asset, not an instrument.

---

## Measured performance

Walk-forward backtest, 14 days of synthetic history, 60/40 split, strictly no target
visible before its validity time. `skill = 1 - MAE/MAE_persistence`.

```
target        lead      MAE  persist     clim   skill  cover
temperature    15m    0.439    0.463    0.449    5.0%    90%
temperature     1h    0.707    0.981    0.840   28.0%    89%
temperature     3h    0.824    1.961    1.302   58.0%    91%
temperature     6h    0.770    3.094    1.619   75.1%    90%
temperature    12h    0.816    3.922    1.969   79.2%    90%
temperature     1d    0.834    1.630    1.683   48.9%    91%

humidity        3h    1.526    2.790    2.826   45.3%    90%
humidity        1d    2.370   14.506   14.523   83.7%    90%

pressure       15m    0.949    0.950    0.950    0.1%    90%   <- persistence wins
pressure        3h    2.606    3.531    3.631   26.2%    89%
pressure        1d    3.389    8.638    8.650   60.8%    90%
```

These are numbers against a simulator, so read them as a check that the machinery is
sound rather than as a promise about your windowsill. Run `scripts/evaluate.py` again
after a fortnight of real data and believe those instead.

---

## Install on the Pi

```bash
sudo apt update && sudo apt install -y python3-venv sense-hat
git clone <your-repo> ~/ashvale-ml && cd ~/ashvale-ml
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install sense-hat smbus2

cp systemd/ashvale.service /etc/systemd/system/   # edit User/paths first
sudo systemctl enable --now ashvale
```

`--system-site-packages` matters: `sense-hat` pulls in `RTIMULib`, which is installed
via apt and is a genuine ordeal to build inside a clean venv.

Without the hardware libraries the suite falls back to a simulated board automatically,
so you can develop the whole thing on a laptop and deploy the same code unchanged.

**Set your altitude in `config.yaml`.** Sea-level pressure reduction is the one setting
people skip and then wonder why every rule-of-thumb forecast reads pessimistic. At 100 m
an uncorrected station pressure shifts the Zambretti number by roughly two categories,
permanently.

---

## The dashboard

Five tabs, one viewport, no scrolling on desktop. Below 1024 px the constraint is
released, because pinning five panels into a phone viewport produces unreadable
eight-pixel type.

| Tab | Answers |
| --- | --- |
| **Live** | The week ahead, current readings, the forecast with its band, and conditions |
| **History** | What did it do, over any timeframe you ask for |
| **Models and Calibration** | Has the model earned its confidence, and the calibration inputs |
| **Stats for Nerds** | Every internal the estimator and the 18 learners are carrying |
| **Methods** | How the whole thing is wired, and how each stage fails |

Live carries the current readings, the observed-and-forecast chart with its 90%
conformal band, and the precipitation panel together, so the question "what is
it doing and what happens next" is answered without changing tab.

### History

Presets from 6 hours to a year, plus an explicit from/to range picker. Aggregation
happens in SQLite, not numpy: pulling 90 days of rows into Python to average them would
cost more memory than the board has. The bucket auto-selects from the span and snaps to
round durations, so 6 hours gives one-minute buckets and a year gives daily ones. Min and
max travel alongside the mean and render as a shaded band, so an hourly view still shows
that the hour spanned four degrees rather than implying a flat line.

Alongside: per-day minima and maxima in local time, all-time records with the timestamp
each was set, and CSV export of any range (streamed as a generator, so a year of history
never has to exist in memory at once).

### Methods

Generated from `methods.py` and rendered against your live config, so it describes the
station you are running rather than the one shipped. Ten stages, each with what it
consumes, what it produces, why it is built that way, and how it fails. The failure mode
is the field that usually goes undocumented and the one you need at 2 a.m.

- **Fan chart** with the 90% conformal band drawn behind the observed line. As the model
  earns confidence the band visibly narrows, so model quality becomes a shape you can
  read from the doorway.
- **Estimator internals**, the signature panel: self-heating coefficient, novelty
  distance and drift pressure, ticking at 2 Hz. Most weather dashboards show numbers;
  this one shows the state estimator working.
- **Conditions ahead**: Zambretti class, rain probability, and the prior/learner/trust
  split so you can see how much the learned model is actually contributing.
- **Two yes/no buttons.** "Was it wet in the last hour?" Each press is a strong label
  worth ten proxy labels. Two seconds of your attention beats a week of heuristics.
- **Calibration box.** Type a thermometer reading, watch `k` update.
- **Scorecard** with skill against persistence, and coverage against the 90% target.
- **Monitors**: novelty, drift pressure, per-sensor health, ensemble weights.

### LED matrix

The 8×8 stopped being a scrolling number. It cycles through glyphs readable across a room:
a pressure-trend arrow coloured by Zambretti class and brightened by tendency magnitude,
a rain-probability column bar, a 3-hour temperature-delta wedge, and a red pulse if a
sensor faults or drift fires. Alerts pre-empt everything, because a six-second scroll is
a six-second delay on the only frame that matters.

---

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/telemetry` | Live reading. **Superset of the original payload**, so existing clients keep working |
| `GET /api/stream` | SSE. One connection instead of a 2-second poll: 0.4% CPU instead of 4% |
| `GET /api/history/range?start=&end=&bucket=` | Any window, SQL-aggregated, auto bucket |
| `GET /api/history/daily?days=` | Per-day min, max and mean in local time |
| `GET /api/records` | All-time extremes, each with its timestamp |
| `GET /api/export.csv?start=&end=` | Streamed CSV export |
| `GET /api/storage` | Rows per resolution tier and database size |
| `GET /api/methods` | The pipeline description the Methods tab renders |
| `GET /api/history?hours=&max_points=` | Decimated history (legacy) |
| `GET /api/forecast?target=` | All horizons with conformal bands and ensemble weights |
| `GET /api/outlook` | Days 2 to 7, climatology plus decaying anomaly, caveat included |
| `GET /api/precipitation` | Zambretti class, rain probability, prior/learner split |
| `GET /api/anomaly` | Novelty, drift, per-sensor health, event log |
| `GET /api/models` | Per-head diagnostics, coverage, precip coefficients |
| `GET /api/scorecard` | Verification: MAE, skill, coverage, sample count |
| `POST /api/train` | Force a retrain |
| `POST /api/verify` | Force a scoring pass |
| `POST /api/label` | `{"kind":"rain","value":1}` strong ground truth |
| `POST /api/calibrate` | `{"reference_c":19.4}` or `{"reset":true}` |
| `GET /api/status` | Hardware, history span, drift, training log |

---

## Tuning

| Symptom | Knob |
| --- | --- |
| Temperature reads consistently high | Calibrate from the dashboard, or raise `sensor.cpu_heat_k` |
| Readings look over-smoothed, lag real changes | Raise `sensor.kalman_q_temp` |
| Rates look noisy | Lower `sensor.kalman_q_*`, or raise `kalman_r_*` |
| Model adapts too slowly to a season change | Lower `model.rls_forgetting` toward 0.995 |
| Model is jumpy and forgets overnight | Raise it toward 0.9995 |
| Coverage sits well below 90% | Raise `model.conformal_gamma` so it corrects faster |
| Drift alarms constantly | Raise `model.drift_lambda` |
| Retrains eat the CPU | Raise `model.train_period_s`, lower `max_pairs` in `NowcastEnsemble.fit` |

A full retrain over 18 heads takes about 10 s on a modern x86 core and closer to 60 to
90 s on a Zero 2 W. It runs in a worker thread, so the sample loop, the API and the LED
never stall while it happens.

---

## Honest limitations

- **Indoors, this forecasts your room, not the sky.** Pressure is the exception: it
  passes through walls, which is why the precipitation model runs on pressure and its
  tendency rather than on your indoor humidity. Set `site.indoors` truthfully.
- **Days 2 to 7 are climatology, not a forecast.** Labelled as such in the API response
  and on the dashboard. They will never catch an incoming Atlantic low, because your
  station physically cannot see one.
- **Rain labels are the bottleneck.** Without a gauge the proxy label is deliberately
  conservative and abstains in the ambiguous middle. The learner earns trust in
  proportion to strong labels: `trust = n / (n + 25)`. Press the buttons.
- **Annual harmonics stay switched off** until 120 days of history exist. Fitting a
  365-day sine to three weeks of data produces a magnificent extrapolation straight off
  the edge of the physical world.
- **One uvicorn worker, deliberately.** The station owns mutable model state; a second
  worker would give you two divergent forecasters sharing a socket.

## Where to take it next

The obvious extensions, roughly in order of payoff per hour of work:

1. **A DS18B20 on a one-metre cable outside the window.** It removes the indoor caveat
   entirely, costs about three pounds, and every model in here improves immediately.
2. **A tipping-bucket rain gauge on a GPIO.** Real precipitation labels turn the logistic
   model from a Zambretti wrapper into something genuinely local.
3. **Pull METAR from a nearby airfield** as a reference channel, and the compensator
   calibrates itself continuously instead of waiting for you to type a number.
4. **Swap the RLS head for an ensemble Kalman filter over the parameter vector** if you
   want proper joint state-parameter estimation. You already have the machinery.
