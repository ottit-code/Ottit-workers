"""Unit tests for lib/warmup_report.py — bucket math, tag stripping, aggregation."""
from unittest.mock import MagicMock, patch

from lib.warmup_report import (
    build_correlation_series,
    build_report_from_rows,
    get_warmup_report,
    normalize_tags,
    normalize_workspace_id,
    persist_warmup_daily_report,
    primary_set_tag,
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
    connection_status="Connected",
    warmup_sent=None,
    warmup_saved_from_spam=None,
    warmup_bounces_caused=None,
    warmup_bounces_received=None,
):
    row = {
        "workspace_id": workspace_id,
        "sender_email_id": sid,
        "sender_email": email or f"s{sid}@{domain}",
        "domain": domain,
        "warmup_enabled": enabled,
        "warmup_score": score,
        "tags": tags if tags is not None else [],
        "connection_status": connection_status,
    }
    if warmup_sent is not None:
        row["warmup_sent"] = warmup_sent
    if warmup_saved_from_spam is not None:
        row["warmup_saved_from_spam"] = warmup_saved_from_spam
    if warmup_bounces_caused is not None:
        row["warmup_bounces_caused"] = warmup_bounces_caused
    if warmup_bounces_received is not None:
        row["warmup_bounces_received"] = warmup_bounces_received
    return row


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
            _row(9, score=0),  # never warmed
            _row(10, score=88, connection_status="Disconnected"),
        ]
        report = build_report_from_rows(
            rows, report_date="2026-08-06", workspace_id=None, source="live"
        )
        assert report["total_accounts"] == 10
        assert report["not_warming"] == 2
        assert report["score_95_plus"] == 2
        assert report["score_90_to_94"] == 2
        assert report["score_below_90"] == 2  # 72 + 88
        assert report["never_warmed"] == 1
        assert report["not_connected"] == 1
        assert report["score_below_95"] == 2 + 2 + 1  # watch + below + never
        assert report["percentages"]["score_95_plus"] == 20.0  # 2/10
        assert report["percentages"]["not_warming"] == 20.0
        assert report["source"] == "live"
        assert report["workspace_id"] is None

        below = report["below_threshold"]
        assert len(below) == 2
        assert {b["sender_email_id"] for b in below} == {"5", "10"}
        assert report["never_warmed_accounts"][0]["sender_email_id"] == "9"
        assert len(report["not_warming_accounts"]) == 2
        assert report["stats"]["perfect_100"] == 0
        assert report["stats"]["lowest_score"] == 72

    def test_below_threshold_excludes_not_warming_and_never_warmed(self):
        rows = [
            _row(1, enabled=False, score=10),
            _row(2, enabled=True, score=10),
            _row(3, enabled=True, score=0),
        ]
        report = build_report_from_rows(
            rows, report_date="2026-08-06", workspace_id="ws_v1"
        )
        assert report["not_warming"] == 1
        assert report["score_below_90"] == 1
        assert report["never_warmed"] == 1
        assert len(report["below_threshold"]) == 1
        assert report["below_threshold"][0]["sender_email_id"] == "2"
        assert report["never_warmed_accounts"][0]["sender_email_id"] == "3"

    def test_by_tag_strips_internal_and_counts_buckets(self):
        rows = [
            _row(1, score=96, tags=["Alpha", "p.Alpha"]),
            _row(2, score=91, tags=[{"name": "Alpha"}, {"name": "p.Alpha"}]),
            _row(3, score=50, tags=["Beta"]),
            _row(4, enabled=False, score=99, tags=["Beta"]),
            _row(5, score=100, tags=[]),  # untagged
        ]
        report = build_report_from_rows(
            rows, report_date="2026-08-06", workspace_id="ws_v1"
        )
        by_tag = {t["tag"]: t for t in report["by_tag"]}
        assert set(by_tag) == {"Alpha", "Beta", "untagged"}
        assert "p.Alpha" not in by_tag
        assert by_tag["Alpha"]["total"] == 2
        assert by_tag["Alpha"]["not_warming"] == 0
        assert by_tag["Alpha"]["score_95_plus"] == 1
        assert by_tag["Alpha"]["score_90_to_94"] == 1
        assert by_tag["Alpha"]["score_below_90"] == 0
        assert by_tag["Alpha"]["below_95"] == 1
        assert by_tag["Alpha"]["avg_score"] == 93.5
        assert by_tag["Beta"]["total"] == 2
        assert by_tag["Beta"]["not_warming"] == 1
        assert by_tag["Beta"]["score_below_90"] == 1
        assert by_tag["untagged"]["score_95_plus"] == 1
        assert report["stats"]["perfect_100"] == 1

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
        assert report["not_warming_accounts"] == []


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
        assert "stats" in upserted["payload"]
        assert "not_warming_accounts" in upserted["payload"]

    def test_get_today_uses_live_performance(self):
        rows = [_row(1, score=96, workspace_id="ws_v1"), _row(2, score=88, workspace_id="ws_v2")]

        def _compute(report_date, workspace_id, supabase):
            if report_date == "2026-08-06":
                return build_report_from_rows(
                    rows, report_date=report_date, workspace_id=None, source="live"
                )
            return build_report_from_rows(
                [], report_date=report_date, workspace_id=None, source="snapshot"
            )

        with (
            patch("lib.warmup_report._today", return_value="2026-08-06"),
            patch("lib.warmup_report._compute_report_for_date", side_effect=_compute),
        ):
            report = get_warmup_report(workspace_id="all", date="2026-08-06")
        assert report["source"] == "live"
        assert report["workspace_id"] is None
        assert report["total_accounts"] == 2
        assert report["score_95_plus"] == 1
        assert report["score_below_90"] == 1
        assert report["previous"] is None
        assert report["delta"] is None

    def test_get_attaches_previous_day_delta(self):
        today_rows = [_row(1, score=96), _row(2, score=88), _row(3, enabled=False)]
        prev_rows = [
            _row(1, score=96),
            _row(2, score=70),
            _row(3, enabled=False),
            _row(4, enabled=False),
        ]

        def _compute(report_date, workspace_id, supabase):
            rows = today_rows if report_date == "2026-08-06" else prev_rows
            return build_report_from_rows(
                rows, report_date=report_date, workspace_id=workspace_id, source="live"
            )

        with (
            patch("lib.warmup_report._today", return_value="2026-08-06"),
            patch("lib.warmup_report._compute_report_for_date", side_effect=_compute),
        ):
            report = get_warmup_report(date="2026-08-06")
        assert report["not_warming"] == 1
        assert report["previous"]["not_warming"] == 2
        assert report["delta"]["not_warming"] == -1
        assert report["score_below_90"] == 1
        assert report["previous"]["score_below_90"] == 1
        assert report["delta"]["score_below_90"] == 0

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
                "never_warmed": 0,
                "not_connected": 0,
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
                        "not_connected": 0,
                        "score_95_plus": 1,
                        "score_90_to_94": 0,
                        "score_below_90": 1,
                        "never_warmed": 0,
                        "below_95": 1,
                        "avg_score": 70.0,
                        "lowest_score": 40,
                    }
                ],
                "stats": {
                    "avg_score_all": 70.0,
                    "avg_score_active": 80.0,
                    "lowest_score": 40,
                    "lowest_email": "a@x.com",
                    "perfect_100": 0,
                    "active_scored": 2,
                },
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
                "never_warmed": 0,
                "not_connected": 0,
                "below_threshold": [],
                "by_tag": [
                    {
                        "tag": "T1",
                        "total": 1,
                        "not_warming": 0,
                        "not_connected": 0,
                        "score_95_plus": 1,
                        "score_90_to_94": 0,
                        "score_below_90": 0,
                        "never_warmed": 0,
                        "below_95": 0,
                        "avg_score": 99.0,
                        "lowest_score": 99,
                    }
                ],
                "stats": {
                    "avg_score_all": 99.0,
                    "avg_score_active": 99.0,
                    "lowest_score": 99,
                    "lowest_email": "b@y.com",
                    "perfect_100": 0,
                    "active_scored": 2,
                },
            },
            "captured_at": "2026-08-01T01:05:00+00:00",
        }

        def _compute(report_date, workspace_id, supabase):
            from lib.warmup_report import (
                _merge_reports,
                _report_from_snapshot_row,
                build_report_from_rows,
            )

            if report_date == "2026-08-01":
                parts = [_report_from_snapshot_row(snap_v1), _report_from_snapshot_row(snap_v2)]
                return _merge_reports(parts, report_date=report_date, source="snapshot")
            return build_report_from_rows(
                [], report_date=report_date, workspace_id=None, source="snapshot"
            )

        with (
            patch("lib.warmup_report._today", return_value="2026-08-06"),
            patch("lib.warmup_report._compute_report_for_date", side_effect=_compute),
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


class TestAccountFieldMapping:
    def test_account_rows_include_bison_parity_fields(self):
        rows = [
            _row(
                1,
                score=40,
                tags=["CI-DED-SET1", "p.CI-DED-SET1"],
                warmup_sent=3,
                warmup_saved_from_spam=2,
                warmup_bounces_caused=1,
                warmup_bounces_received=0,
            ),
            _row(2, enabled=False, score=99, tags=["Alpha"], warmup_sent=50),
            _row(3, score=0, tags=[], warmup_sent=0),
        ]
        report = build_report_from_rows(
            rows, report_date="2026-08-06", workspace_id="ws_v1"
        )
        below = report["below_threshold"][0]
        assert below["email"] == "s1@ex.com"
        assert below["warmup_score"] == 40
        assert below["score"] == 40
        assert below["warmup_sent"] == 3
        assert below["spam_saves"] == 2
        assert below["set"] == "CI-DED-SET1"
        assert below["tags"] == ["CI-DED-SET1"]
        assert below["bounces_caused"] == 1
        assert below["bounces_received"] == 0

        nw = report["not_warming_accounts"][0]
        assert nw["warmup_sent"] == 50
        assert nw["set"] == "Alpha"
        assert nw["score"] == 99

        never = report["never_warmed_accounts"][0]
        assert never["warmup_sent"] == 0
        assert never["set"] is None

    def test_never_warmed_uses_warmup_sent_zero(self):
        """Enabled + warmup_sent == 0 is never_warmed even if score looks live."""
        rows = [
            _row(1, score=88, warmup_sent=0),  # never warmed by sends
            _row(2, score=0, warmup_sent=None),  # fallback: score == 0
            _row(3, score=88, warmup_sent=5),  # below 90, actively warming
        ]
        report = build_report_from_rows(
            rows, report_date="2026-08-06", workspace_id="ws_v1"
        )
        assert report["never_warmed"] == 2
        assert report["score_below_90"] == 1
        assert {a["sender_email_id"] for a in report["never_warmed_accounts"]} == {
            "1",
            "2",
        }
        assert report["below_threshold"][0]["sender_email_id"] == "3"
        assert report["below_threshold"][0]["warmup_sent"] == 5

    def test_primary_set_tag(self):
        assert primary_set_tag(["CI-DED-Set4", "extra"]) == "CI-DED-Set4"
        assert primary_set_tag([]) is None


class TestCorrelationSeries:
    def test_joins_warmup_and_reply_stats_by_date(self):
        warmup = [
            {
                "report_date": "2026-08-01",
                "total_accounts": 100,
                "score_95_plus": 80,
                "avg_warmup_score": 96.5,
            },
            {
                "report_date": "2026-08-02",
                "total_accounts": 100,
                "score_95_plus": 90,
                "payload": {
                    "stats": {"avg_score_active": 97.1, "avg_score_all": 96.0}
                },
            },
        ]
        stats = [
            {"stat_date": "2026-08-01", "emails_sent": 1000, "emails_replied": 50},
            {"stat_date": "2026-08-02", "emails_sent": 2000, "emails_replied": 80},
            {"stat_date": "2026-08-03", "emails_sent": 500, "emails_replied": 10},
        ]
        series = build_correlation_series(warmup, stats)
        assert [p["date"] for p in series] == [
            "2026-08-01",
            "2026-08-02",
            "2026-08-03",
        ]
        d1 = series[0]
        assert d1["avg_warmup_score"] == 96.5
        assert d1["pct_95_plus"] == 80.0
        assert d1["emails_sent"] == 1000
        assert d1["replies"] == 50
        assert d1["reply_rate"] == 5.0

        d2 = series[1]
        assert d2["avg_warmup_score"] == 97.1
        assert d2["pct_95_plus"] == 90.0
        assert d2["reply_rate"] == 4.0

        d3 = series[2]
        assert d3["avg_warmup_score"] is None
        assert d3["pct_95_plus"] is None
        assert d3["reply_rate"] == 2.0

    def test_aggregates_multi_workspace_rows(self):
        warmup = [
            {
                "report_date": "2026-08-01",
                "total_accounts": 50,
                "score_95_plus": 40,
                "avg_warmup_score": 95.0,
            },
            {
                "report_date": "2026-08-01",
                "total_accounts": 50,
                "score_95_plus": 50,
                "avg_warmup_score": 99.0,
            },
        ]
        stats = [
            {"stat_date": "2026-08-01", "emails_sent": 100, "emails_replied": 10},
            {"stat_date": "2026-08-01", "emails_sent": 300, "emails_replied": 30},
        ]
        series = build_correlation_series(warmup, stats)
        assert len(series) == 1
        p = series[0]
        assert p["pct_95_plus"] == 90.0  # 90/100
        assert p["avg_warmup_score"] == 97.0  # (95*50 + 99*50) / 100
        assert p["emails_sent"] == 400
        assert p["replies"] == 40
        assert p["reply_rate"] == 10.0
