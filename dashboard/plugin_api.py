"""The Mocks tab's backend, mounted by Hermes Desktop at /api/plugins/mock-review/ inside the gateway process.

A thin adapter: every route calls mockreview.api and turns a ReviewError into an HTTP error carrying its message and
status. This is the only file of the package that imports fastapi. Importing it starts no thread, socket or subprocess.
It never serves the board and never changes the board's HTTP surface: the Desktop reads reviews through these routes
and, to comment or approve, opens the board in the browser.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException

REPO = Path(__file__).resolve().parent.parent

router = APIRouter()


def _api():
    if str(REPO) not in sys.path and (REPO / "mockreview" / "__init__.py").is_file():
        sys.path.insert(0, str(REPO))
    import mockreview.api as api
    from mockreview.store import ReviewError
    return api, ReviewError


def _call(fn: Callable[..., Any], *args: Any) -> Any:
    api, ReviewError = _api()
    try:
        return fn(api, *args)
    except ReviewError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


@router.get("/reviews")
def reviews() -> dict:
    """Every review, closed and older ones included, newest activity first."""
    return {"reviews": _call(lambda api: api.desk_list())}


@router.get("/reviews/{review_id}/view")
def view(review_id: str, round: Optional[int] = None) -> dict:
    """One round (default: the current one) as a read-only, sanitised `data:` URL document plus the header facts."""
    return _call(lambda api, rid, n: api.desk_view(rid, n), review_id, round)


@router.post("/reviews/{review_id}/delete")
def delete(review_id: str) -> dict:
    """Delete a review for good (an open one is closed first). The Desktop asks for confirmation before calling."""
    return _call(lambda api, rid: api.delete_review(rid), review_id)
