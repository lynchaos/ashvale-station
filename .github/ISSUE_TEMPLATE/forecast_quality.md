---
name: Forecast quality
about: The models are producing poor or strange forecasts
labels: forecasting
---

**What looks wrong**

**Output of `python scripts/evaluate.py`**

```
```

**Scorecard from the Models and calibration tab** (or `GET /api/scorecard`)

```json
```

**How long has the station been logging?** (`history_days` from `/api/status`)

**Is it indoors?** And is `site.altitude_m` set correctly in your config?
These two account for most reported forecast oddities.
