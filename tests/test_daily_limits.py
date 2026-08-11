"""Unit tests for lib/daily_limits.py — set parsing, KPIs, bundles, preview."""
from lib.daily_limits import (
    build_bundles,
    compute_kpis,
    extract_set_indexes,
    normalize_sender_row,
    preview_change,
    tag_counts,
    utilization,
)


class TestExtractSetIndexes:
    def test_common_bison_tags(self):
        assert extract_set_indexes(["SM-GOOG-0609-Set1"]) == [1]
        assert extract_set_indexes(["CI-DED-Set4"]) == [4]
        assert extract_set_indexes(["IN-S001-Set2", "extra"]) == [2]

    def test_multiple_and_dedupe(self):
        assert extract_set_indexes(["A-Set1", "B-Set3", "A-Set1"]) == [1, 3]

    def test_ignores_out_of_range(self):
        assert extract_set_indexes(["Bundle-Set7", "Set0"]) == []

    def test_empty(self):
        assert extract_set_indexes([]) == []


class TestKpis:
    def test_utilization_buckets(self):
        senders = [
            {"daily_limit": 10, "sent_today": 10, "tags": ["A-Set1"]},  # at limit
            {"daily_limit": 10, "sent_today": 10, "tags": []},  # at limit
            {"daily_limit": 20, "sent_today": 19, "tags": ["B"]},  # settling (95%)
            {"daily_limit": 10, "sent_today": 6, "tags": []},  # >50%
            {"daily_limit": 10, "sent_today": 2, "tags": []},  # low
            {"daily_limit": 5, "sent_today": None, "tags": ["C"]},  # unknown util
        ]
        kpis = compute_kpis(senders)
        assert kpis["total_daily_limit"] == 65
        assert kpis["senders_at_limit"] == 2
        assert kpis["settling_users"] == 1
        assert kpis["senders_over_50"] == 4  # 2 at-limit + settling + 6/10
        assert kpis["sender_count"] == 6
        assert kpis["bundled_sender_count"] == 3


class TestBundlesAndTags:
    def test_heatmap_counts(self):
        senders = [
            normalize_sender_row(
                {
                    "sender_email_id": 1,
                    "workspace_id": "ws_v1",
                    "daily_limit": 20,
                    "tags": ["SM-GOOG-Set1"],
                },
                sent_today=0,
            ),
            normalize_sender_row(
                {
                    "sender_email_id": 2,
                    "workspace_id": "ws_v1",
                    "daily_limit": 20,
                    "tags": ["SM-GOOG-Set1", "Also-Set4"],
                },
                sent_today=0,
            ),
            normalize_sender_row(
                {
                    "sender_email_id": 3,
                    "workspace_id": "ws_v1",
                    "daily_limit": 1,
                    "tags": [],
                },
                sent_today=0,
            ),
        ]
        bundles = build_bundles(senders)
        by_lim = {b["daily_limit"]: b for b in bundles}
        assert by_lim[20]["count"] == 2
        assert by_lim[20]["set_1"] == 2
        assert by_lim[20]["set_4"] == 1
        assert by_lim[1]["count"] == 1
        assert by_lim[1]["set_1"] == 0

        tags = tag_counts(senders)
        names = {t["tag"] for t in tags}
        assert "SM-GOOG-Set1" in names
        assert "Also-Set4" in names


class TestPreview:
    def test_set_to(self):
        selected = [
            {"sender_email_id": "1", "workspace_id": "ws_v1", "daily_limit": 10},
            {"sender_email_id": "2", "workspace_id": "ws_v1", "daily_limit": 20},
        ]
        preview = preview_change(selected, "set", 25)
        assert preview["capacity_now"] == 30
        assert preview["capacity_after"] == 50
        assert preview["capacity_change"] == 20
        assert preview["target_by_limit"] == [{"daily_limit": 25, "count": 2}]

    def test_increase_decrease(self):
        selected = [
            {"sender_email_id": "1", "workspace_id": "ws_v1", "daily_limit": 10},
        ]
        assert preview_change(selected, "increase", 5)["updates"][0]["to"] == 15
        assert preview_change(selected, "decrease", 20)["updates"][0]["to"] == 0


class TestUtilization:
    def test_none_when_unknown(self):
        assert utilization(None, 10) is None
        assert utilization(5, 0) is None
        assert utilization(5, 10) == 0.5
