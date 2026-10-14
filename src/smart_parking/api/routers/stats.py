"""Analytics endpoints (doc section E, "Analytics and system")."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from smart_parking.api import deps
from smart_parking.api.deps import ApiError
from smart_parking.api.schemas import DurationStatsResponse, OccupancySeriesResponse, naive_utc
from smart_parking.services import stats as stat_math
from smart_parking.db.base import utcnow

router = APIRouter(prefix="/stats", tags=["stats"])

# A window nobody asked for is still a window: an unbounded "since the beginning"
# chart is a query that gets slower every month of operation, and a caller who
# wants more can ask for more.
DEFAULT_WINDOW = timedelta(hours=24)
MAX_WINDOW = timedelta(days=92)


def _window(from_: datetime | None, to: datetime | None) -> tuple[datetime, datetime]:
    end = naive_utc(to) or utcnow()
    start = naive_utc(from_) or (end - DEFAULT_WINDOW)
    if start >= end:
        raise ApiError(422, "empty_window", "from must be before to")
    if end - start > MAX_WINDOW:
        raise ApiError(422, "window_too_wide", f"this endpoint reads up to {MAX_WINDOW.days} days at a time")
    return start, end


@router.get("/occupancy", response_model=OccupancySeriesResponse)
def occupancy(
    user: deps.UserDep,
    repos: deps.ReposDep,
    camera_id: str | None = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    bucket: str = "hour",
) -> OccupancySeriesResponse:
    """Occupancy over time, sampled at each bucket's start.

    The series counts bays whose **recorded interval** covered the instant, from
    `slot_occupancy_history`. That table is written when an interval closes, so
    the bay occupied right now has no row yet: its truth is the live status on
    `/lots/{id}/summary`, and a history chart that pretended to include it would
    be reporting a state the FSM has not committed.
    """
    if bucket not in stat_math.BUCKET_SECONDS:
        raise ApiError(422, "unknown_bucket", f"bucket must be one of {sorted(stat_math.BUCKET_SECONDS)}")
    start, end = _window(from_, to)
    rows = repos.occupancy.intervals_between(start, end, camera_id)
    # `ended_at IS NULL` is carried through as None rather than coerced to a time:
    # an interval that has not closed is not a claim about when it did.
    intervals = [(row.started_at, row.ended_at) for row, _slot in rows]
    samples = stat_math.bucket_starts(start, end, bucket)
    return OccupancySeriesResponse(
        camera_id=camera_id,
        bucket=bucket,
        intervals_considered=len(intervals),
        points=stat_math.occupancy_series(intervals, samples),
    )


@router.get("/duration", response_model=DurationStatsResponse)
def duration(
    user: deps.UserDep,
    repos: deps.ReposDep,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
) -> DurationStatsResponse:
    """Average and median stay, plus the busiest hours of the day.

    `sample_size` is in the body on purpose. An average over four visits and an
    average over forty look identical without it, and the one number a report
    repeats is the one that needs its denominator attached.
    """
    start, end = _window(from_, to)
    values = repos.sessions.durations(start, end)
    summary = stat_math.duration_stats(values)
    arrivals = repos.sessions.entry_times(start, end)
    peaks = stat_math.peak_hours(arrivals)
    return DurationStatsResponse(
        **summary,
        arrivals_considered=len(arrivals),
        peak_hours=peaks,
    )


@router.get("/revenue", dependencies=[Depends(deps.current_user)])
def revenue(days: int = Query(default=7, ge=1, le=92)) -> dict:
    """Refused, not answered with a zero: tariffs and payments are a later phase.

    An empty series would read as "no revenue" in any client that charts it, and
    this system has no revenue *data* - which is a different claim. The dashboard
    treats the 501 as "nothing to chart yet" and hides the section; the guard
    test in test_api_endpoints.py keeps this refusal honest.
    """
    raise ApiError(
        501,
        "not_implemented",
        "revenue reporting needs tariffs and payments, which are a later phase",
    )
