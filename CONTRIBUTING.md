# Contributing to Ashvale Station

Thanks for taking an interest. This is a small project maintained by one person,
so the bar here is "make it easy to say yes", not "follow a 40-page process".

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

## Before you open a pull request

- [ ] `python scripts/evaluate.py` runs clean, and you have posted before/after numbers
- [ ] Those numbers came from a backfill with `--seed` and `--end` both pinned
- [ ] Coverage on the scorecard is still near target for anything you touched
- [ ] No new required dependencies
- [ ] New model code explains its failure mode in the docstring
- [ ] The dashboard still fits one viewport at 1280x800 if you changed the UI

## Good first contributions

- **DS18B20 or BME280 support.** An outdoor sensor removes the single biggest
  limitation in the project. High impact, self-contained.
- **Tipping-bucket rain gauge on GPIO.** Real precipitation labels would
  transform the precipitation model.
- **METAR ingestion** from a nearby airfield as a calibration reference.
- **Translations** for the dashboard.
- **Tests.** There is a walk-forward backtest but no unit test suite. A pytest
  suite over `physics.py`, `estimation.py` and `models/rls.py` would be very welcome.

## Reporting bugs

Open an issue with your `config.yaml` (redact coordinates if you like), the
output of `GET /api/status`, and what you expected instead. If it is a
forecasting problem rather than a crash, the output of `scripts/evaluate.py`
helps enormously.

## Licensing of contributions

By contributing you agree that your work is licensed under the Apache License
2.0, the same terms as the project. You keep the copyright in your own
contributions.
