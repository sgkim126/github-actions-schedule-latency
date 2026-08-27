# GitHub Actions Schedule Latency

This repository measures how long GitHub Actions `schedule` events are delayed.

## Measurement

- Run interval: 5 minutes
- GitHub Actions is assumed to trigger each scheduled run within 24 hours.
- The 288 five-minute slots in a day use distinct cron expressions so that the original scheduled hour and minute can be recovered from `github.event.schedule`.

The scheduled date is reconstructed from the observation time. Runs delayed by 24 hours or more are not supported because their original date cannot be determined reliably, and they are treated as missing.

## Raw Observations

Raw observations are stored on the `measurements` branch.
Each scheduled time has one append-only JSON Lines file named `<hour>_<minute>.jsonl`. For example, `0 1 * * *` is stored in `observations/1_0.jsonl`.

Each observation contains two fields:

| Field | Description |
| --- | --- |
| `scheduled_at_utc` | The time intended by the cron schedule |
| `observed_at_utc` | The time when the runner executed its first command |

`observed_at_utc` is captured when the runner executes its first shell command. The measurement therefore includes the time GitHub takes to create the scheduled event and the time spent waiting for a GitHub-hosted runner, but excludes checkout and Python startup time.

Latency is calculated during aggregation and is not duplicated in the raw observation.

If multiple observations have the same `scheduled_at_utc`, aggregation keeps the earliest `observed_at_utc` so that re-runs do not inflate the measured latency.

If the remote `measurements` branch advances, the workflow rebuilds its commit from the latest remote branch and appends the originally captured observation again. The captured `observed_at_utc` does not change during retries.

## Aggregated Statistics

Aggregated statistics are stored in `data/summary.csv` on the `main` branch.
The aggregation workflow runs daily at `01:26 UTC` and can also be run manually.

The file contains one row for each of the 288 five-minute schedule slots, followed by `recent_1d`, `recent_3d`, `recent_7d`, `recent_15d`, `recent_30d`, and `overall` rows. Each row includes:

- Count and mean latency
- Nearest-rank p50, p95, and p99 latency
- Minimum and maximum latency
- Cumulative percentages at 1 minute, 5 minutes, 15 minutes, 30 minutes, and 1 hour
- Percentage over 1 hour
- Finalized expected and observed counts
- Missing count and missing percentage

The aggregation commit message records the UTC time through which missing data has been finalized.

Recent-period statistics exclude the most recent 24 hours to allow delayed runs to arrive.

### Missing observations

Missing statistics consider only scheduled times more than 24 hours in the past to avoid marking delayed runs as missing while they may still arrive.

Expected and missing counts start at the earliest recorded `scheduled_at_utc`.

### Statistics push conflicts

If the remote `main` branch advances, the workflow rebuilds the statistics from the latest `main` and retries the push. It makes at most five attempts and retries only when the remote branch has advanced.
