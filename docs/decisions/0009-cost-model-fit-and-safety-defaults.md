# 0009 — Cost model: what is fitted, and how it fails safe

- Status: accepted · Date: 2026-11-25

## Context
Plan §5.3 asks for a regression from tokens to percent of a limit, per model and bucket, with fixed
price-ratio token weights and NNLS-fitted per-model coefficients, refit nightly, R² stored, and wider
margins when R² < 0.7. Several details were open.

## Decision
1. **Unit of fit = model family** (`haiku`, `sonnet`, `opus`, `other`), one fit per bucket. Token-kind weights
   stay fixed at the price-ratio prior shared with `collector/split.py` (in 1, out 5, cache write 1.25,
   cache read 0.1); only `a_m` (percent per weighted token) is fitted, with a Lawson–Hanson NNLS in numpy
   (no SciPy dependency). Columns are norm-scaled before solving.
2. **Windows** are consecutive readings of the same bucket, at most 6 h apart, whose reported `resets_at`
   agree within 5 min (manual readings have no reset and are never paired), whose percent did not drop, and
   that contain logged tokens in `(start, end]`. When several sources report at the same instant the highest
   reading wins.
3. **Flagging.** A fit is `low_confidence` when R² < 0.7 **or** it has fewer than 8 windows. R² is the usual
   1 − SS_res/SS_tot (around the mean). The flag, R², window counts and reasons are stored per fit
   (`cost_model` table, migration 0005; one row per family).
4. **Never "free".** A family with no data, or whose NNLS coefficient is 0, gets the price-ratio prior scaled
   from the best-supported fitted family (e.g. sonnet = 3/5 × opus) and is marked `source=prior`. Unknown model
   families use the `other` coefficient, priced like the top tier. A fit without any positive coefficient
   stores nothing, so consumers keep the previous fit or treat the model as missing.
5. **Refit** every 24 h on the last 28 days, from the collector loop; an empty attempt also counts, so it is
   not retried every tick.

## Consequences
- The fitted percent includes usage Claude Code cannot see (claude.ai chat etc.). That shows up as a lower R²
  and therefore wider margins, not as a silent bias. Filtering windows with invisible usage is a follow-up.
- For model-scoped buckets (`weekly_model:<name>`) the prior charges other families too; this over-estimates
  agent spend, which is the safe direction.
