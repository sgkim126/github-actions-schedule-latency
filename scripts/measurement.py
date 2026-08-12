#!/usr/bin/env python3
"""Build statistics from checked-out GitHub Actions schedule observations."""

from __future__ import annotations

import argparse
import csv
import json
import math
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence


UTC = timezone.utc
SCHEDULE_INTERVAL_MINUTES = 5
RAW_FIELDS = [
    "scheduled_at_utc",
    "observed_at_utc",
]

SUMMARY_FIELDS = [
    "slot_utc",
    "count",
    "mean_seconds",
    "p50_seconds",
    "p95_seconds",
    "p99_seconds",
    "min_seconds",
    "max_seconds",
    "within_1m_percent",
    "within_5m_percent",
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
            "mean_seconds": "",
            "p50_seconds": "",
            "p95_seconds": "",
            "p99_seconds": "",
            "min_seconds": "",
            "max_seconds": "",
            "within_1m_percent": "",
            "within_5m_percent": "",
            "within_15m_percent": "",
            "within_30m_percent": "",
            "within_1h_percent": "",
            "over_1h_percent": "",
        }

    sorted_values = sorted(values)
    count = len(sorted_values)
    within = {
        threshold: bisect_right(sorted_values, threshold)
        for threshold in (60, 300, 900, 1800, 3600)
    }
    return {
        "count": count,
        "mean_seconds": f"{sum(sorted_values) / count:.2f}",
        "p50_seconds": str(int(nearest_rank(sorted_values, 0.50))),
        "p95_seconds": str(int(nearest_rank(sorted_values, 0.95))),
        "p99_seconds": str(int(nearest_rank(sorted_values, 0.99))),
        "min_seconds": str(int(sorted_values[0])),
        "max_seconds": str(int(sorted_values[-1])),
        "within_1m_percent": formatted_percent(within[60], count),
        "within_5m_percent": formatted_percent(within[300], count),
        "within_15m_percent": formatted_percent(within[900], count),
        "within_30m_percent": formatted_percent(within[1800], count),
        "within_1h_percent": formatted_percent(within[3600], count),
        "over_1h_percent": formatted_percent(count - within[3600], count),
    }


def expected_by_slot(start_at: datetime, finalized_through: datetime) -> dict[str, int]:
    cutoff = max(finalized_through, start_at)
    counts: dict[str, int] = defaultdict(int)
    current = start_at
    step = timedelta(minutes=SCHEDULE_INTERVAL_MINUTES)
    while current < cutoff:
        counts[current.strftime("%H%M")] += 1
        current += step
    return counts


def summary_row(
    slot: str,
    observations: Sequence[Mapping[str, str]],
    finalized_observations: Sequence[Mapping[str, str]],
    finalized_expected: int,
    finalized_through: datetime,
) -> dict[str, str | int]:
    stats = latency_statistics(
        [
            (parse_timestamp(row["observed_at_utc"]) - parse_timestamp(row["scheduled_at_utc"])).total_seconds()
            for row in observations
        ]
    )
    finalized_observed = len(finalized_observations)
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
    start_at = parse_timestamp(observations[0]["scheduled_at_utc"])
    finalized_through = max(finalized_through, start_at)
    expected = expected_by_slot(start_at, finalized_through)
    finalized = [row for row in observations if parse_timestamp(row["scheduled_at_utc"]) < finalized_through]

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    grouped_finalized: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in observations:
        grouped[parse_timestamp(row["scheduled_at_utc"]).strftime("%H%M")].append(row)
    for row in finalized:
        grouped_finalized[parse_timestamp(row["scheduled_at_utc"]).strftime("%H%M")].append(row)

    slots = [
        f"{hour:02d}{minute:02d}"
        for hour in range(24)
        for minute in range(0, 60, SCHEDULE_INTERVAL_MINUTES)
    ]
    rows = [
        summary_row(slot, grouped[slot], grouped_finalized[slot], expected[slot], finalized_through)
        for slot in slots
    ]
    rows.append(
        summary_row(
            "overall",
            observations,
            finalized,
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
