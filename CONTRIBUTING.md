# Contributing to Ashvale Station

Read this first, so nobody wastes an afternoon.

**This is a solo project.** It is written and maintained by one person, for one
weather station, and it is published because the methods may be useful to
someone else, not because it is looking for a team.

**Bug reports are genuinely welcome.** If something crashes, forecasts badly, or
the documentation is wrong, open an issue. That is useful and I will read it.

**Pull requests are unlikely to be merged.** Not from lack of gratitude: this
codebase carries a lot of hard-won reasoning in its comments and docstrings, and
reviewing changes to it properly costs more time than I have. If you want it to
do something different, fork it. Apache 2.0 exists precisely so you can.

The rest of this file documents how the project holds itself to a standard. It
is written for anyone reading the code, including future me.

## Ground rules that actually matter

**No heavy dependencies.** The whole point is that this runs on a Raspberry Pi
Zero 2 W with 512 MB of RAM. Pull requests adding PyTorch, TensorFlow, pandas or
scikit-learn to the core will be declined, however elegant. If a model genuinely
needs one, make it an optional extra with a numpy fallback.

**Claims need numbers.** If you say a change improves forecasting, show the
output of `scripts/evaluate.py` before and after, on the same data. Skill against
persistence is the metric that counts. "It looks better" is not evidence.

"The same data" means bit-identical, which takes two flags, not one. `--seed`
alone is not enough: the synthetic history is anchored to wall clock, so the OU
realisation repeats while the timestamps shift, and that moves solar elevation,
day of year and the seasonal harmonic. Those feed the temperature and humidity
models directly, so two same-seed runs give you different data and an
uninterpretable comparison. Pin both:

```bash
python scripts/simulate.py --days 21 --wipe --seed 11 --end 1767225600
python scripts/evaluate.py --train-frac 0.6 --hours 100000
```

The wide `--hours` matters: `evaluate.py` looks back from now, and its default
window is 60 days, so a history pinned to a timestamp further in the past than
that falls outside it and the script reports "Not enough history".

**Uncertainty must stay calibrated.** If you touch the forecasting path, check
that coverage on the scorecard still sits near the target. A model that gets more
accurate while its intervals start lying is a regression, not an improvement.

**Document how it fails.** Every model module carries a docstring explaining not
just what the technique is but how it breaks in the field. Keep that up. It is
the most useful part of this codebase.

## Getting set up

```bash
git clone https://github.com/lynchaos/ashvale-station.git
cd ashvale-station
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python scripts/simulate.py --days 21 --wipe   # synthetic history
python scripts/evaluate.py                    # baseline numbers
python run.py --no-led                        # dashboard on :8000
```

No Sense HAT needed. The simulator kicks in automatically and exercises every
code path.

## The bar any change has to clear

Whether it is my own commit or a fork of yours, a change to the forecasting
path is not finished until it can show:

- [ ] `python scripts/evaluate.py` runs clean, and you have posted before/after numbers
- [ ] Those numbers came from a backfill with `--seed` and `--end` both pinned
- [ ] Coverage on the scorecard is still near target for anything you touched
- [ ] No new required dependencies
- [ ] New model code explains its failure mode in the docstring
- [ ] The dashboard still fits one viewport at 1280x800 if you changed the UI

## Roadmap

Where this is going, in rough order of value. Listed so a forker knows what is
already planned rather than as an invitation.

- **DS18B20 or BME280 support.** An outdoor sensor removes the single biggest
  limitation in the project. High impact, self-contained.
- **Tipping-bucket rain gauge on GPIO.** Real precipitation labels would
  transform the precipitation model.
- **METAR ingestion** from a nearby airfield as a calibration reference.
- **Tests.** There is a walk-forward backtest but no unit test suite. A pytest
  suite over `physics.py`, `estimation.py` and `models/rls.py` is the main gap.

## Reporting bugs

Open an issue with your `config.yaml` (redact coordinates if you like), the
output of `GET /api/status`, and what you expected instead. If it is a
forecasting problem rather than a crash, the output of `scripts/evaluate.py`
helps enormously.

## Licensing

The project is Apache 2.0. Fork it, modify it, ship it, subject to the licence
terms. In the unlikely event a patch is accepted, it is taken under the same
terms and you keep the copyright in your own work.
