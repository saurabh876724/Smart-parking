"""Plate search (doc section E, "Sessions and vehicles")."""

from __future__ import annotations

from fastapi import APIRouter, Request

from smart_parking.api import deps
from smart_parking.api.deps import ApiError, ctx_of
from smart_parking.api.schemas import PlatePage, PlateResponse

router = APIRouter(prefix="/plates", tags=["plates"])


@router.get("/search", response_model=PlatePage)
def search(
    request: Request,
    user: deps.UserDep,
    repos: deps.ReposDep,
    q: str = "",
    limit: int | None = None,
    offset: int = 0,
) -> PlatePage:
    """Substring search over the plates the system has read - masked for everyone.

    The doc's response column says "masked results", and that rule is kept even
    for the roles that may see a full plate elsewhere. A search is a way to walk
    a table a hundred rows at a time; the place a plate is *disclosed* is a
    session an operator is working on, where the audit trail names them. Here,
    `plate` is always absent and `plate_masked` always present, and the masked
    form is enough to recognise a number you already know.

    A fragment of one character is refused: `%A%` is not a search, it is a dump.
    """
    ctx = ctx_of(request)
    # Measured on the *normalised* fragment. `%%%` is three characters and
    # normalises to nothing, which as a LIKE pattern is a match for everything:
    # the length guard has to sit in front of the text the query will actually
    # use, or three symbols are a way to walk the table.
    fragment = ctx.normalise(q)
    if len(fragment) < 3:
        raise ApiError(422, "query_too_short", "search needs at least three plate characters")
    size = ctx.cfg.api.page_size if limit is None else max(1, min(limit, ctx.cfg.api.page_size))
    rows, total = repos.plates.search(fragment, limit=size, offset=max(0, offset))
    return PlatePage(
        total=total,
        limit=size,
        offset=max(0, offset),
        items=[PlateResponse.from_row(row, reveal_plates=False) for row in rows],
    )
