"""Tests for prompts/templates.py — especially the canonical snapshot rendering.

The lead_engagement_snapshots row shape comes from Supabase exactly as the
canonical query returns it (see lib/lead_enricher.py):

    engagement_score, funnel_stage, campaign_engagements, tags, status

Where:
  - campaign_engagements: list[{campaign_id, status, emails_sent, opens, replies, interested, ...}]
  - tags: list[{id, name, default, created_at, updated_at}]
"""
from prompts import templates


def test_format_snapshot_renders_canonical_columns():
    snap = {
        "engagement_score": 7,
        "funnel_stage": "replied",
        "status": "verified",
        "tags": [
            {"id": 14, "name": "Outlook", "default": True},
            {"id": 22, "name": "Shopify", "default": False},
        ],
        "campaign_engagements": [
            {"campaign_id": 124, "status": "active", "emails_sent": 5, "opens": 3, "replies": 1, "interested": True},
            {"campaign_id": 99,  "status": "stopped", "emails_sent": 2, "opens": 0, "replies": 0, "interested": False},
        ],
    }
    lines = templates._format_snapshot(snap)
    out = "\n".join(lines)

    assert "engagement_score: 7" in out
    assert "funnel_stage: replied" in out
    assert "status: verified" in out
    assert "tags: Outlook, Shopify" in out
    assert "campaign=124" in out
    assert "status=active" in out
    assert "interested=true" in out
    assert "campaign=99" in out
    assert "status=stopped" in out
    # No noisy timestamps from the raw payload should leak into the prompt.
    assert "created_at" not in out
    assert "updated_at" not in out


def test_format_snapshot_skips_missing_fields():
    """Missing/empty columns should not produce blank lines."""
    snap = {"engagement_score": 0, "funnel_stage": "uploaded"}
    lines = templates._format_snapshot(snap)
    out = "\n".join(lines)

    assert "engagement_score: 0" in out
    assert "funnel_stage: uploaded" in out
    assert "tags" not in out
    assert "campaigns" not in out
    assert "status" not in out


def test_format_snapshot_handles_empty_lists():
    snap = {
        "engagement_score": 1,
        "funnel_stage": "uploaded",
        "tags": [],
        "campaign_engagements": [],
    }
    lines = templates._format_snapshot(snap)
    out = "\n".join(lines)
    assert "tags" not in out
    assert "campaigns" not in out


def test_format_snapshot_tolerates_malformed_tag_entries():
    """Non-dict or name-less entries should be silently dropped, not crash."""
    snap = {
        "engagement_score": 1,
        "tags": [{"id": 1}, "garbage", {"name": "Real"}, None],
    }
    lines = templates._format_snapshot(snap)
    out = "\n".join(lines)
    assert "tags: Real" in out


def test_summarize_engagement_compact_format():
    e = {
        "campaign_id": 7,
        "status": "active",
        "emails_sent": 10,
        "opens": 4,
        "replies": 2,
        "interested": False,
    }
    line = templates._summarize_engagement(e)
    assert line == "campaign=7 status=active sent=10 opens=4 replies=2"
