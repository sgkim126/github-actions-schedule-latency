#!/usr/bin/env python3
"""Build statistics from checked-out GitHub Actions schedule observations."""

from __future__ import annotations

import argparse
import csv
import json
import math
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence


UTC = timezone.utc
SCHEDULE_INTERVAL_MINUTES = 5
RECENT_WINDOWS = [
    ("recent_1d", timedelta(days=1)),
    ("recent_3d", timedelta(days=3)),
    ("recent_7d", timedelta(days=7)),
    ("recent_15d", timedelta(days=15)),
    ("recent_30d", timedelta(days=30)),
]
RAW_FIELDS = [
    "scheduled_at_utc",
    "observed_at_utc",
]

SUMMARY_FIELDS = [
    "slot_utc",
    "count",
    "min_seconds",
    "p25_seconds",
    "p50_seconds",
    "p75_seconds",
    "p95_seconds",
    "p99_seconds",
    "max_seconds",
    "within_1m_percent",
    "within_15m_percent",
    "within_30m_percent",
    "within_1h_percent",
    "over_1h_percent",
    "finalized_expected_count",
    "finalized_observed_count",
    "missing_count",
    "missing_percent",
    "finalized_through_utc",
]


def load_observations(directory: Path) -> list[dict[str, object]]:
    observations: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.jsonl")):
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                observation = json.loads(line)
                if not isinstance(observation, dict):
                    raise ValueError(f"{path}:{line_number} must contain a JSON object")
                observations.append(observation)
    return observations


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include a timezone: {value}")
    return parsed.astimezone(UTC)


def write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def deduplicate_observations(
    observations: Iterable[Mapping[str, object]],
) -> list[dict[str, str]]:
    by_schedule: dict[str, dict[str, str]] = {}
    for raw in observations:
        missing = [field for field in RAW_FIELDS if field not in raw]
        if missing:
            raise ValueError(f"observation is missing fields: {', '.join(missing)}")
        observation = {field: str(raw[field]) for field in RAW_FIELDS}
        parse_timestamp(observation["scheduled_at_utc"])
        observed_at = parse_timestamp(observation["observed_at_utc"])

        existing = by_schedule.get(observation["scheduled_at_utc"])
        if existing is None or observed_at < parse_timestamp(existing["observed_at_utc"]):
            by_schedule[observation["scheduled_at_utc"]] = observation

    return sorted(by_schedule.values(), key=lambda row: row["scheduled_at_utc"])


def nearest_rank(sorted_values: Sequence[float], percentile: float) -> float:
    if not sorted_values:
        raise ValueError("cannot calculate a percentile without values")
    index = max(0, math.ceil(percentile * len(sorted_values)) - 1)
    return sorted_values[index]


def formatted_percent(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return ""
    return f"{numerator * 100 / denominator:.2f}"


def latency_statistics(values: Sequence[float]) -> dict[str, str | int]:
    if not values:
        return {
            "count": 0,
            "min_seconds": "",
            "p25_seconds": "",
            "p50_seconds": "",
            "p75_seconds": "",
            "p95_seconds": "",
            "p99_seconds": "",
            "max_seconds": "",
            "within_1m_percent": "",
            "within_15m_percent": "",
            "within_30m_percent": "",
            "within_1h_percent": "",
            "over_1h_percent": "",
        }

    sorted_values = sorted(values)
    count = len(sorted_values)
    within = {
        threshold: bisect_right(sorted_values, threshold)
        for threshold in (60, 900, 1800, 3600)
    }
    return {
        "count": count,
        "min_seconds": str(int(sorted_values[0])),
        "p25_seconds": str(int(nearest_rank(sorted_values, 0.25))),
        "p50_seconds": str(int(nearest_rank(sorted_values, 0.50))),
        "p75_seconds": str(int(nearest_rank(sorted_values, 0.75))),
        "p95_seconds": str(int(nearest_rank(sorted_values, 0.95))),
        "p99_seconds": str(int(nearest_rank(sorted_values, 0.99))),
        "max_seconds": str(int(sorted_values[-1])),
        "within_1m_percent": formatted_percent(within[60], count),
        "within_15m_percent": formatted_percent(within[900], count),
        "within_30m_percent": formatted_percent(within[1800], count),
        "within_1h_percent": formatted_percent(within[3600], count),
        "over_1h_percent": formatted_percent(count - within[3600], count),
    }


def expected_by_slot(start_at: datetime, finalized_through: datetime) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    step = timedelta(minutes=SCHEDULE_INTERVAL_MINUTES)
    first = start_at.replace(second=0, microsecond=0)
    minute_remainder = first.minute % SCHEDULE_INTERVAL_MINUTES
    if minute_remainder:
        first += timedelta(minutes=SCHEDULE_INTERVAL_MINUTES - minute_remainder)
    if first < start_at:
        first += step
    if first >= finalized_through:
        return counts

    occurrence_count = math.ceil((finalized_through - first) / step)
    slot_count = 24 * 60 // SCHEDULE_INTERVAL_MINUTES
    full_days, remaining = divmod(occurrence_count, slot_count)
    first_slot = (first.hour * 60 + first.minute) // SCHEDULE_INTERVAL_MINUTES
    for offset in range(slot_count):
        count = full_days + (1 if offset < remaining else 0)
        if count:
            slot = (first_slot + offset) % slot_count
            hour, minute_index = divmod(slot, 60 // SCHEDULE_INTERVAL_MINUTES)
            counts[f"{hour:02d}{minute_index * SCHEDULE_INTERVAL_MINUTES:02d}"] = count
    return counts


def expected_count(start_at: datetime, end_at: datetime) -> int:
    return sum(expected_by_slot(start_at, end_at).values())


def summary_row_from_latencies(
    slot: str,
    latencies: Sequence[float],
    finalized_observed: int,
    finalized_expected: int,
    finalized_through: datetime,
) -> dict[str, str | int]:
    stats = latency_statistics(latencies)
    missing = max(0, finalized_expected - finalized_observed)
    return {
        "slot_utc": slot,
        **stats,
        "finalized_expected_count": finalized_expected,
        "finalized_observed_count": finalized_observed,
        "missing_count": missing,
        "missing_percent": formatted_percent(missing, finalized_expected),
        "finalized_through_utc": finalized_through.astimezone(UTC).isoformat(timespec="milliseconds"),
    }


def rebuild_summaries(
    root: Path,
    now: datetime,
    observations: Iterable[Mapping[str, object]],
) -> None:
    finalized_through = now - timedelta(days=1)
    observations = deduplicate_observations(observations)
    scheduled_times = [parse_timestamp(row["scheduled_at_utc"]) for row in observations]
    latencies = [
        (parse_timestamp(row["observed_at_utc"]) - scheduled_at).total_seconds()
        for row, scheduled_at in zip(observations, scheduled_times, strict=True)
    ]
    start_at = scheduled_times[0]
    finalized_through = max(finalized_through, start_at)
    expected = expected_by_slot(start_at, finalized_through)
    finalized_end = bisect_left(scheduled_times, finalized_through)

    grouped_latencies: dict[str, list[float]] = defaultdict(list)
    grouped_finalized_counts: dict[str, int] = defaultdict(int)
    for index, (scheduled_at, latency) in enumerate(zip(scheduled_times, latencies, strict=True)):
        slot = scheduled_at.strftime("%H%M")
        grouped_latencies[slot].append(latency)
        if index < finalized_end:
            grouped_finalized_counts[slot] += 1

    slots = [
        f"{hour:02d}{minute:02d}"
        for hour in range(24)
        for minute in range(0, 60, SCHEDULE_INTERVAL_MINUTES)
    ]
    rows = [
        summary_row_from_latencies(
            slot,
            grouped_latencies[slot],
            grouped_finalized_counts[slot],
            expected[slot],
            finalized_through,
        )
        for slot in slots
    ]
    for label, window in RECENT_WINDOWS:
        window_start = max(start_at, finalized_through - window)
        window_start_index = bisect_left(scheduled_times, window_start, 0, finalized_end)
        window_latencies = latencies[window_start_index:finalized_end]
        rows.append(
            summary_row_from_latencies(
                label,
                window_latencies,
                len(window_latencies),
                expected_count(window_start, finalized_through),
                finalized_through,
            )
        )
    rows.append(
        summary_row_from_latencies(
            "overall",
            latencies,
            finalized_end,
            sum(expected.values()),
            finalized_through,
        )
    )
    write_csv(root / "data" / "summary.csv", SUMMARY_FIELDS, rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--now", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    observations = load_observations(args.observations)
    rebuild_summaries(
        args.root.resolve(),
        parse_timestamp(args.now),
        observations,
    )
    print(f"observations={len(observations)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
