"""Aggregates over the rows Phase 7 persisted (Phase 8's `/stats`).

Pure functions on lists, deliberately: an endpoint that computes a statistic in
its handler cannot be tested without HTTP, and a statistic nobody tests is a
number a report repeats. The SQL stays in the repositories - this module takes
rows and does arithmetic.

Every function here answers one question in one stated way. "Occupancy over
time" has at least three reasonable definitions, and picking one silently is how
two dashboards end up disagreeing about the same afternoon.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from typing import Iterable

BUCKET_SECONDS = {"hour": 3600, "day": 86400}


def bucket_starts(from_ts: datetime, to_ts: datetime, bucket: str = "hour") -> list[datetime]:
    """The instants to sample at: each bucket's own start, from `from_ts` forward."""
    step = BUCKET_SECONDS[bucket]
    start = from_ts.replace(second=0, microsecond=0)
    if bucket == "day":
        start = start.replace(hour=0)
    if start < from_ts:
        # The first sample is the window's start, not a bucket before it: nothing
        # outside the window is being claimed.
        start = from_ts
    out = []
    while start <= to_ts:
        out.append(start)
        start = start + timedelta(seconds=step)
        if bucket == "hour":
            # Re-align after a jump that crossed an hour boundary by an odd
            # remainder, so the series is on the clock, not on the request time.
            start = start.replace(second=0, microsecond=0)
    return out


def occupancy_at(intervals: Iterable[tuple[datetime, datetime | None]], when: datetime) -> int:
    """How many bays were occupied at one instant.

    An interval that ends exactly at `when` has released the bay, so its end is
    exclusive; a still-running interval (`ended_at IS NULL`) counts in every
    window it started in - that is what the row says.
    """
    return sum(1 for started, ended in intervals if started <= when and (ended is None or ended > when))


def occupancy_series(
    intervals: Iterable[tuple[datetime, datetime | None]],
    samples: list[datetime],
) -> list[dict]:
    """`[{at, occupied}]`, one point per sample."""
    rows = list(intervals)
    return [{"at": sample.isoformat(), "occupied": occupancy_at(rows, sample)} for sample in samples]


def duration_stats(values: list[float]) -> dict:
    """Average, median, longest and the sample size, over measured durations only.

    `average` without `sample_size` next to it is a number that hides how much of
    the table it came from, so the count is part of the result rather than a
    caller's problem.
    """
    if not values:
        return {"average_s": None, "median_s": None, "longest_s": None, "sample_size": 0}
    return {
        "average_s": sum(values) / len(values),
        "median_s": median(values),
        "longest_s": max(values),
        "sample_size": len(values),
    }


def peak_hours(times: list[datetime], top: int = 3) -> list[dict]:
    """Busiest hours of the day by arrivals, most crowded first.

    Hour *of day*, not hour of the record: "when does this lot get busy" is a
    question about the clock, and answering it with 2026-09-28T14:00 buckets would
    need a week of data before it meant anything.
    """
    counts: dict[int, int] = {}
    for moment in times:
        counts[moment.hour] = counts.get(moment.hour, 0) + 1
    ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return [{"hour_of_day": hour, "arrivals": count} for hour, count in ordered[:top]]
