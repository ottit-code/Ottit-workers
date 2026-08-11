"""Closed-identity plan progress math for Daily Review.

PLAN / SENT / LEFT / % must stay coherent for ops:
  sent_of_plan = min(sent, plan)
  left         = max(plan - sent_of_plan, 0)
  pct          = sent_of_plan / plan * 100

Bison's emails_being_sent (sending-schedules) is a schedule-size aggregate
that does not drain like "remaining of today's plan" — never use it as LEFT
when plan + sent are known.
"""
from __future__ import annotations

from typing import Optional, Tuple


def sent_of_plan(plan: Optional[int], sent: Optional[int]) -> Optional[int]:
    if plan is None or sent is None:
        return None
    plan_i = max(0, int(plan))
    sent_i = max(0, int(sent))
    return min(sent_i, plan_i)


def left_of_plan(plan: Optional[int], sent: Optional[int]) -> Optional[int]:
    """Emails still to send for today's plan (drains to 0 as the day completes)."""
    sop = sent_of_plan(plan, sent)
    if sop is None or plan is None:
        return None
    return max(int(plan) - sop, 0)


def pct_of_plan(plan: Optional[int], sent: Optional[int]) -> Optional[float]:
    """Completed portion of plan (0–100). Never 100% while left ≈ plan."""
    if plan is None or sent is None:
        return None
    plan_i = int(plan)
    if plan_i <= 0:
        return None
    sop = sent_of_plan(plan_i, sent)
    assert sop is not None
    return (sop / plan_i) * 100.0


def progress(
    plan: Optional[int], sent: Optional[int]
) -> Tuple[Optional[int], Optional[int], Optional[float]]:
    """Return (sent_of_plan, left, pct)."""
    return sent_of_plan(plan, sent), left_of_plan(plan, sent), pct_of_plan(plan, sent)
