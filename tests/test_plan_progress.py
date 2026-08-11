"""Closed-identity PLAN / SENT / LEFT / % math for Daily Review."""
from __future__ import annotations

from lib import plan_progress


def test_abdullah_aug10_example_sent_above_plan():
    """PLAN 3835, SENT 3839 must not show LEFT≈PLAN with 100%."""
    plan, sent = 3835, 3839
    assert plan_progress.sent_of_plan(plan, sent) == 3835
    assert plan_progress.left_of_plan(plan, sent) == 0
    assert plan_progress.pct_of_plan(plan, sent) == 100.0


def test_partial_day_drains_left():
    plan, sent = 1000, 400
    assert plan_progress.left_of_plan(plan, sent) == 600
    assert plan_progress.pct_of_plan(plan, sent) == 40.0


def test_none_inputs():
    assert plan_progress.left_of_plan(None, 10) is None
    assert plan_progress.left_of_plan(10, None) is None
    assert plan_progress.pct_of_plan(0, 5) is None
