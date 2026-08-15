# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-08-15

First public release.

### Added
- Multi-horizon forecasting: 18 direct heads (3 targets x 6 horizons) using
  exponentially weighted recursive least squares with a capped covariance trace.
- Hedge-blended ensemble over persistence, climatology and the learned model,
  so the system can conclude that the learned model is not worth using.
- Adaptive conformal prediction intervals with coverage feedback.
- Constant-velocity Kalman bank (Joseph form) for level and rate estimation.
- Grey-box CPU self-heating compensation with an RLS-estimated coefficient,
  calibrated from a single trusted thermometer reading.
- Harmonic climatology with anomaly decay for a 7-day outlook, with annual
  terms gated behind 120 days of history.
- Precipitation model: Zambretti barometric prior plus an online logistic
  residual learner with human-in-the-loop labelling.
- Monitoring: Mahalanobis EWMA novelty, Page-Hinkley drift detection that
  triggers retraining, and stuck-sensor detection.
- Verification loop scoring every matured forecast against persistence and
  climatology, surfaced as a public scorecard.
- Tiered storage: raw to 5-minute to hourly downsampling, roughly 30 MB per year.
- Arbitrary-range history queries, per-day summaries, all-time records and
  streamed CSV export.
- Five-tab dashboard sized to a single viewport, with a Methods tab generated
  from the live configuration.
- LED matrix driver with pressure-trend arrows, rain bars and alert pulses.
- Physics-based simulator fallback so the suite runs without a Sense HAT.
- Backfill and walk-forward backtest scripts.
