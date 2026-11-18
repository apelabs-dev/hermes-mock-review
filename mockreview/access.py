"""The one access rule for a review. Pure: no database, no request, so it is unit-tested as a table.

The idea follows Excaliself's share-link resolver (src/features/shares/access.ts at 96b922c, MIT, Copyright (c) 2026
Maciej Zygmunt): one pure function, and every failure that could reveal whether a review exists collapses into one
"dead" result. This is a re-implementation for mock-review's roles, not a copy.

Every route that touches a review asks resolve() and then can() for the capability it needs. The server reads the
principal's role and grant fresh on each request and passes them in here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Optional

# What each role on a review may do. "owner" is the install's admins (and the loopback owner): everything.
CAPS = {
    "viewer": frozenset({"read"}),
    "commenter": frozenset({"read", "comment"}),
    "reviewer": frozenset({"read", "comment", "submit"}),
    "owner": frozenset({"read", "comment", "submit", "manage"}),
}
ROLE_ORDER = ("viewer", "commenter", "reviewer", "owner")

OK, SIGNIN, DEAD = "ok", "signin", "dead"


@dataclass(frozen=True)
class Principal:
    """Who is asking. kind: owner (an admin account), member, guest or anonymous."""
    kind: str
    user_id: Optional[int] = None
    guest_review: Optional[str] = None      # a guest's review ...
    guest_generation: Optional[int] = None  # ... and the share-link generation they joined through

    @property
    def signed_in(self) -> bool:
        return self.kind != "anonymous"


ANONYMOUS = Principal("anonymous")


@dataclass(frozen=True)
class Link:
    """A review's live share link (None when it has none or it is stopped or expired)."""
    generation: int
    guest_role: str


@dataclass(frozen=True)
class Decision:
    result: str                      # ok | signin | dead
    role: Optional[str] = None       # the effective role when ok

    @property
    def caps(self) -> FrozenSet[str]:
        return CAPS.get(self.role or "", frozenset())


def resolve(principal: Principal, review_id: str, review_exists: bool, grant: Optional[str] = None,
            link: Optional[Link] = None) -> Decision:
    """The access decision for one request to one review.

    - Nobody signed in: always "signin", whether or not the review exists (the sign-in page reveals nothing).
    - Signed in but no way in, or the review is gone: "dead", identical for both.
    - An admin: owner of every review. A member: their live grant. A guest: the link's guest role, only while the
      link is live and still the generation they joined through.
    """
    if not principal.signed_in:
        return Decision(SIGNIN)
    if not review_exists:
        return Decision(DEAD)
    if principal.kind == "owner":
        return Decision(OK, "owner")
    if principal.kind == "member":
        return Decision(OK, grant) if grant in CAPS else Decision(DEAD)
    if principal.kind == "guest":
        if (link is not None and principal.guest_review == review_id and principal.guest_generation == link.generation
                and link.guest_role in CAPS):
            return Decision(OK, link.guest_role)
        return Decision(DEAD)
    return Decision(DEAD)


def can(decision: Decision, cap: str) -> bool:
    return decision.result == OK and cap in decision.caps
