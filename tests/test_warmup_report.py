"""Unit tests for lib/warmup_report.py — bucket math, tag stripping, aggregation."""
from unittest.mock import MagicMock, patch

from lib.warmup_report import (
    build_report_from_rows,
    get_warmup_report,
    normalize_tags,
    normalize_workspace_id,
    persist_warmup_daily_report,
)


def _row(
    sid,
    *,
    email=None,
    domain="ex.com",
    enabled=True,
    score=95,
    tags=None,
    workspace_id="ws_v1",
):
    return {
        "workspace_id": workspace_id,
        "sender_email_id": sid,
        "sender_email": email or f"s{sid}@{domain}",
        "domain": domain,
        "warmup_enabled": enabled,
        "warmup_score": score,
        "tags": tags if tags is not None else [],
    }


class TestNormalizeTags:
    def test_strips_internal_p_dot_tags(self):
        raw = ["CI-DED-Set4", "p.CI-DED-Set4", {"name": "Bundle-A"}, {"name": "p.Bundle-A"}]
        assert normalize_tags(raw) == ["CI-DED-Set4", "Bundle-A"]

    def test_empty_and_none(self):
        assert normalize_tags(None) == []
        assert normalize_tags([]) == []


class TestNormalizeWorkspaceId:
    def test_all_and_empty_mean_aggregate(self):
        assert normalize_workspace_id(None) is None
        assert normalize_workspace_id("all") is None
        assert normalize_workspace_id("") is None

    def test_keeps_explicit_workspace(self):
        assert normalize_workspace_id("ws_v2") == "ws_v2"


class TestBuildReportFromRows:
    def test_slack_buckets_and_percentages(self):
        rows = [
            _row(1, score=99),   # 95+
            _row(2, score=95),   # 95+
            _row(3, score=92),   # 90-94
            _row(4, score=90),   # 90-94
            _row(5, score=72),   # below 90
            _row(6, enabled=False, score=99),  # not warming
            _row(7, enabled=None, score=50),   # not warming (null)
            _row(8, enabled=True, score=None),  # enabled+unscored → total only
        ]
        report = build_report_from_rows(
            rows, report_date="2026-08-06", workspace_id=None, source="live"
        )
        assert report["total_accounts"] == 8
        assert report["not_warming"] == 2
        assert report["score_95_plus"] == 2
        assert report["score_90_to_94"] == 2
        assert report["score_below_90"] == 1
        assert report["percentages"]["score_95_plus"] == 25.0  # 2/8
        assert report["percentages"]["not_warming"] == 25.0
        assert report["source"] == "live"
        assert report["workspace_id"] is None

        below = report["below_threshold"]
        assert len(below) == 1
        assert below[0]["sender_email_id"] == "5"
        assert below[0]["warmup_score"] == 72
        assert below[0]["email"] == "s5@ex.com"

    def test_below_threshold_excludes_not_warming(self):
        rows = [
            _row(1, enabled=False, score=10),
            _row(2, enabled=True, score=10),
        ]
        report = build_report_from_rows(
            rows, report_date="2026-08-06", workspace_id="ws_v1"
        )
        assert report["not_warming"] == 1
        assert report["score_below_90"] == 1
        assert len(report["below_threshold"]) == 1
        assert report["below_threshold"][0]["sender_email_id"] == "2"

    def test_by_tag_strips_internal_and_counts_buckets(self):
        rows = [
            _row(1, score=96, tags=["Alpha", "p.Alpha"]),
            _row(2, score=91, tags=[{"name": "Alpha"}, {"name": "p.Alpha"}]),
            _row(3, score=50, tags=["Beta"]),
            _row(4, enabled=False, score=99, tags=["Beta"]),
        ]
        report = build_report_from_rows(
            rows, report_date="2026-08-06", workspace_id="ws_v1"
        )
        by_tag = {t["tag"]: t for t in report["by_tag"]}
        assert set(by_tag) == {"Alpha", "Beta"}
        assert "p.Alpha" not in by_tag
        assert by_tag["Alpha"] == {
            "tag": "Alpha",
            "total": 2,
            "not_warming": 0,
            "score_95_plus": 1,
            "score_90_to_94": 1,
            "score_below_90": 0,
        }
        assert by_tag["Beta"]["total"] == 2
        assert by_tag["Beta"]["not_warming"] == 1
        assert by_tag["Beta"]["score_below_90"] == 1

        # below_threshold tags also stripped
        assert report["below_threshold"][0]["tags"] == ["Beta"]

    def test_empty_rows(self):
        report = build_report_from_rows(
            [], report_date="2026-08-06", workspace_id="ws_v2"
        )
        assert report["total_accounts"] == 0
        assert report["percentages"]["score_95_plus"] == 0.0
        assert report["below_threshold"] == []
        assert report["by_tag"] == []


class TestPersistAndGet:
    def test_persist_upserts_payload(self):
        sb = MagicMock()
        rows = [_row(1, score=99), _row(2, score=70)]
        report = persist_warmup_daily_report(
            "ws_v1", "2026-08-06", rows=rows, supabase=sb
        )
        assert report is not None
        assert report["score_95_plus"] == 1
        assert report["score_below_90"] == 1
        sb.table.assert_called_with("warmup_daily_report")
        upserted = sb.table.return_value.upsert.call_args[0][0]
        assert upserted["workspace_id"] == "ws_v1"
        assert upserted["report_date"] == "2026-08-06"
        assert upserted["total_accounts"] == 2
        assert "below_threshold" in upserted["payload"]
        assert "by_tag" in upserted["payload"]

    def test_get_today_uses_live_performance(self):
        rows = [_row(1, score=96, workspace_id="ws_v1"), _row(2, score=88, workspace_id="ws_v2")]
        with (
            patch("lib.warmup_report._today", return_value="2026-08-06"),
            patch("lib.warmup_report.fetch_performance_rows", return_value=rows) as fetch,
        ):
            report = get_warmup_report(workspace_id="all", date="2026-08-06")
        fetch.assert_called_once()
        assert report["source"] == "live"
        assert report["workspace_id"] is None
        assert report["total_accounts"] == 2
        assert report["score_95_plus"] == 1
        assert report["score_below_90"] == 1

    def test_get_historical_merges_workspace_snapshots(self):
        snap_v1 = {
            "workspace_id": "ws_v1",
            "report_date": "2026-08-01",
            "total_accounts": 3,
            "not_warming": 1,
            "score_95_plus": 1,
            "score_90_to_94": 0,
            "score_below_90": 1,
            "payload": {
                "below_threshold": [
                    {
                        "sender_email_id": "9",
                        "email": "a@x.com",
                        "domain": "x.com",
                        "warmup_score": 40,
                        "tags": ["T1"],
                    }
                ],
                "by_tag": [
                    {
                        "tag": "T1",
                        "total": 2,
                        "not_warming": 0,
                        "score_95_plus": 1,
                        "score_90_to_94": 0,
                        "score_below_90": 1,
                    }
                ],
            },
            "captured_at": "2026-08-01T01:00:00+00:00",
        }
        snap_v2 = {
            "workspace_id": "ws_v2",
            "report_date": "2026-08-01",
            "total_accounts": 2,
            "not_warming": 0,
            "score_95_plus": 2,
            "score_90_to_94": 0,
            "score_below_90": 0,
            "payload": {
                "below_threshold": [],
                "by_tag": [
                    {
                        "tag": "T1",
                        "total": 1,
                        "not_warming": 0,
                        "score_95_plus": 1,
                        "score_90_to_94": 0,
                        "score_below_90": 0,
                    }
                ],
            },
            "captured_at": "2026-08-01T01:05:00+00:00",
        }
        with (
            patch("lib.warmup_report._today", return_value="2026-08-06"),
            patch(
                "lib.warmup_report.fetch_snapshots",
                return_value=[snap_v1, snap_v2],
            ),
        ):
            report = get_warmup_report(date="2026-08-01")
        assert report["source"] == "snapshot"
        assert report["workspace_id"] is None
        assert report["total_accounts"] == 5
        assert report["not_warming"] == 1
        assert report["score_95_plus"] == 3
        assert report["score_below_90"] == 1
        assert len(report["below_threshold"]) == 1
        t1 = next(t for t in report["by_tag"] if t["tag"] == "T1")
        assert t1["total"] == 3
        assert t1["score_95_plus"] == 2
        assert t1["score_below_90"] == 1
