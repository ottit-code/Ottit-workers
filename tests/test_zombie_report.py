"""Unit tests for lib/zombie_report.py — bins, set tags, watchlist, payload."""

from lib.zombie_report import (
    WATCH_MIN_SENDS,
    build_watchlist,
    build_zombie_payload,
    canonicalize_set,
    flagged_by,
    flat_day_triples,
    pick_set_tag,
    rate_bin,
    reply_rate,
    stats_in_window,
)


class TestBinsAndRate:
    def test_rate_bin_boundaries(self):
        assert rate_bin(0.5) == 0
        assert rate_bin(1.0) == 1
        assert rate_bin(1.24) == 1
        assert rate_bin(1.25) == 2
        assert rate_bin(1.99) == 3
        assert rate_bin(2.0) == 4
        assert rate_bin(3.0) == 5

    def test_reply_rate(self):
        assert reply_rate(0, 0) == 0.0
        assert reply_rate(100, 3) == 3.0


class TestSetTags:
    def test_pick_prefers_set_tag(self):
        assert pick_set_tag(["warmup", "CI-DED-Set4", "other"]) == "CI-DED-Set4"

    def test_canonicalize_strips_date_segments(self):
        assert canonicalize_set("SM-GOOG-0609-Set1") == ("SM-GOOG-Set1", "SM-GOOG")
        assert canonicalize_set("CI-DED-Set4-0518") == ("CI-DED-Set4", "CI-DED")
        assert canonicalize_set("CI-DED-Set4") == ("CI-DED-Set4", "CI-DED")


class TestTriplesAndWindow:
    def test_flat_triples_sparse(self):
        days = ["2026-01-01", "2026-01-02", "2026-01-03"]
        per = {"2026-01-01": (10, 1), "2026-01-03": (5, 0)}
        assert flat_day_triples(days, per) == [0, 10, 1, 2, 5, 0]

    def test_stats_in_window(self):
        triples = [0, 10, 1, 1, 20, 0, 2, 30, 2]
        st = stats_in_window(triples, 1, 2)
        assert st["s"] == 50
        assert st["r"] == 2
        assert st["rt"] == 4.0


class TestWatchlist:
    def test_flagged_window_needs_volume_and_low_rate(self):
        low = {"s": WATCH_MIN_SENDS, "r": 0, "rt": 0.0, "rb": 0, "cb": 0}
        assert flagged_by(low, "7")
        thin = {"s": 10, "r": 0, "rt": 0.0, "rb": 0, "cb": 0}
        assert not flagged_by(thin, "7")

    def test_flagged_all_time_reply_count_arm(self):
        # Under 4 replies flags on all-time even with decent rate bin if rb<=1 OR r<4
        few = {"s": 100, "r": 3, "rt": 3.0, "rb": 5, "cb": 0}
        assert flagged_by(few, "all")

    def test_build_watchlist_multi_basis(self):
        days = [f"2026-01-{i:02d}" for i in range(1, 31)]
        # Dead inbox: 50 sends / 0 replies every day → flags all windows
        triples = []
        for i in range(30):
            triples.extend([i, 50, 0])
        payload = build_zombie_payload(
            days=days,
            inbox_rows=[
                {
                    "email": "dead@ex.com",
                    "tags": ["CI-DED-Set6"],
                    "daily_limit": 20,
                    "contacted": 100,
                    "bounced": 1,
                    "daily": {d: {"sent": 50, "replies": 0} for d in days},
                },
                {
                    "email": "healthy@ex.com",
                    "tags": ["CI-DED-Set4"],
                    "daily_limit": 20,
                    "contacted": 100,
                    "bounced": 0,
                    "daily": {d: {"sent": 50, "replies": 3} for d in days},
                },
            ],
        )
        watch = build_watchlist(payload["sets"], payload["days"], basis="7")
        emails = {r["e"] for r in watch}
        assert "dead@ex.com" in emails
        assert "healthy@ex.com" not in emails
        dead = next(r for r in watch if r["e"] == "dead@ex.com")
        assert dead["hits"] == 4


class TestPayload:
    def test_sets_and_totals(self):
        days = ["2026-06-01", "2026-06-02"]
        payload = build_zombie_payload(
            days=days,
            inbox_rows=[
                {
                    "email": "a@ex.com",
                    "tags": ["SM-GOOG-0609-Set1"],
                    "daily_limit": 15,
                    "contacted": 10,
                    "bounced": 0,
                    "daily": {
                        "2026-06-01": {"sent": 10, "replies": 1},
                        "2026-06-02": {"sent": 10, "replies": 0},
                    },
                }
            ],
        )
        assert payload["days"] == days
        assert len(payload["sets"]) == 1
        assert payload["sets"][0]["name"] == "SM-GOOG-Set1"
        assert payload["sets"][0]["family"] == "SM-GOOG"
        assert payload["totals"]["sent"] == 20
        assert payload["totals"]["rep"] == 1
        cell = payload["sets"][0]["cells"][0]
        assert cell["d"] == [0, 10, 1, 1, 10, 0]
        assert cell["dl"] == 15
