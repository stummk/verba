"""The learned spellings: read them, correct them, judge them.

Every route acts in the caller's own scope (`learning.scope_for_user`), which
is what keeps a correction out of a private transcript from reaching anyone
else's vocabulary. Without user management — or with `learning.shared` on —
that scope is the whole installation, and everyone sees the same list.

No administrator rights: a user's own corrected spellings are theirs, and
they are applied to their own transcriptions.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import config
from ..services import learning
from .deps import CurrentUser

router = APIRouter(prefix="/api/learning", tags=["learning"])


class TermUpdate(BaseModel):
    """What may be changed about one entry.

    The corrected form, because the learning can only ever propose what it
    saw; and the verdict, which always wins over the evidence — "rejected"
    stays rejected however often the correction is made again.
    """

    target: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["proposed", "accepted", "rejected", "retired"] | None = None


@router.get("/terms")
def list_terms(user: dict = CurrentUser) -> dict:
    """The vocabulary of the caller's scope, best attested first."""
    settings = config.get_settings().learning
    return {
        "terms": learning.list_terms(learning.scope_for_user(user)),
        "enabled": settings.enabled,
        "min_evidence": settings.min_evidence,
    }


@router.put("/terms/{term_id}")
def update_term(term_id: int, body: TermUpdate, user: dict = CurrentUser) -> dict:
    term = learning.update_term(
        term_id,
        learning.scope_for_user(user),
        target=body.target,
        status=body.status,
    )
    if term is None:
        raise HTTPException(status_code=404, detail="Dieser Eintrag existiert nicht")
    return term


@router.delete("/terms/{term_id}")
def delete_term(term_id: int, user: dict = CurrentUser) -> dict:
    if not learning.delete_term(term_id, learning.scope_for_user(user)):
        raise HTTPException(status_code=404, detail="Dieser Eintrag existiert nicht")
    return {"deleted": True}


class RuleUpdate(BaseModel):
    """What may be changed about one distilled rule.

    The wording, because a rule is a sentence a model wrote and the person it
    applies to may put it better; and the verdict, which decides whether it is
    used at all.
    """

    text: str | None = Field(default=None, min_length=1, max_length=500)
    status: Literal["proposed", "accepted", "rejected", "retired"] | None = None


@router.get("/rules")
def list_rules(user: dict = CurrentUser) -> dict:
    """The rules distilled from the caller's corrections, best attested first."""
    settings = config.get_settings().learning
    return {
        "rules": learning.list_rules(learning.scope_for_user(user)),
        "enabled": settings.enabled and settings.rules_enabled,
        "steps": list(learning.RULE_STEPS),
    }


@router.put("/rules/{rule_id}")
def update_rule(rule_id: int, body: RuleUpdate, user: dict = CurrentUser) -> dict:
    rule = learning.update_rule(
        rule_id, learning.scope_for_user(user), text=body.text, status=body.status
    )
    if rule is None:
        raise HTTPException(status_code=404, detail="Diese Regel existiert nicht")
    return rule


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int, user: dict = CurrentUser) -> dict:
    if not learning.delete_rule(rule_id, learning.scope_for_user(user)):
        raise HTTPException(status_code=404, detail="Diese Regel existiert nicht")
    return {"deleted": True}
