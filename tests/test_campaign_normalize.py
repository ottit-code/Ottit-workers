"""Campaign list mapping — reply rate must match EmailBison."""
from api.routers.campaigns import _normalize_campaign


def test_normalize_uses_unique_replies_and_contacted():
    row = _normalize_campaign(
        {
            "id": 10,
            "name": "CI-DED",
            "status": "Active",
            "emails_sent": 36223,
            "replied": 400,
            "unique_replies": 386,
            "bounced": 0,
            "total_leads": 15071,
            "total_leads_contacted": 14804,
            "completion_percentage": 80,
        }
    )
    assert row["emails_sent_count"] == 36223
    assert row["reply_count"] == 386
    assert row["total_leads"] == 15071
    assert row["total_leads_contacted"] == 14804
    assert row["campaign_status"] == "active"
    # Bison: 386 / 14,804 = 2.61%
    assert round(row["reply_count"] / row["total_leads_contacted"] * 100, 2) == 2.61


def test_normalize_falls_back_to_replied_when_unique_missing():
    row = _normalize_campaign({"id": 1, "replied": 12, "emails_sent": 100})
    assert row["reply_count"] == 12
    assert row["total_leads_contacted"] == 0
