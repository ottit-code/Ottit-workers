"""
campaign_daily_stats_poller.py — runs daily at midnight (or every 6 hours)

Fetches per-campaign time-series stats from EmailBison and upserts into Supabase:
- campaign_daily_stats: per-campaign, per-date email engagement metrics

Backfill strategy:
  First run  → fetches from campaign created_at date (full history)
  Subsequent → fetches last 2 days only (incremental)
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx

from lib import emailbison
from lib.config import DEFAULT_WORKSPACE_ID, pollable_workspaces
from lib.supabase_client import get_supabase
from lib.utils import get_active_campaign_ids, get_active_campaign_ids_from_bison

logger = logging.getLogger(__name__)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _yesterday() -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()


def _safe_rate(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return round((numerator / denominator) * 100, 4)


def _get_start_date(supabase, campaign_id: str, campaign_details: dict) -> str:
    """Return the chart-stats start_date for this campaign.

    Uses yesterday for incremental runs; falls back to campaign created_at
    on first run (no rows in campaign_daily_stats for this campaign yet).
    """
    try:
        result = (
            supabase.table("campaign_daily_stats")
            .select("stat_date")
            .eq("campaign_id", campaign_id)
            .limit(1)
            .execute()
        )
        if result.data:
            return _yesterday()
    except Exception as e:
        logger.warning(f"Could not check existing stats for campaign {campaign_id}: {e}")

    # First run — backfill from campaign creation date
    created_at = campaign_details.get("created_at") or _yesterday()
    # Normalise to YYYY-MM-DD if a full timestamp was returned
    created_str = str(created_at)
    if "T" in created_str:
        created_str = created_str.split("T")[0]
    return created_str


def _parse_chart_stats(raw: dict | list) -> dict[str, dict]:
    """Parse chart stats API response into {date_str: {col: value}} mapping."""
    series: list = []
    if isinstance(raw, dict):
        series = raw.get("data", [])
    elif isinstance(raw, list):
        series = raw

    label_to_col = {
        "Sent": "emails_sent",
        "Total Opens": "emails_opened",
        "Unique Opens": "unique_opens",
        "Replied": "emails_replied",
        "Bounced": "emails_bounced",
        "Unsubscribed": "unsubscribed",
        "Interested": "interested",
    }

    by_date: dict[str, dict] = {}
    for item in series:
        col = label_to_col.get(item.get("label", ""))
        if not col:
            continue
        for date_str, count in item.get("dates", []):
            if date_str not in by_date:
                by_date[date_str] = {
                    "emails_sent": 0, "emails_opened": 0, "unique_opens": 0,
                    "emails_replied": 0, "emails_bounced": 0,
                    "unsubscribed": 0, "interested": 0,
                }
            by_date[date_str][col] = int(count or 0)
    return by_date


def poll_campaign_daily_stats(
    workspace_id: str = DEFAULT_WORKSPACE_ID,
    bison: emailbison.BisonClient | None = None,
) -> None:
    """Fetch daily stats for all active campaigns and upsert into campaign_daily_stats."""
    supabase = get_supabase()
    today = _today()
    bison = bison or emailbison.for_workspace(workspace_id)
    if workspace_id == DEFAULT_WORKSPACE_ID:
        campaign_ids = get_active_campaign_ids(supabase)
    else:
        campaign_ids = get_active_campaign_ids_from_bison(bison)
    logger.info(f"[{workspace_id}] Polling daily stats for {len(campaign_ids)} campaigns")

    all_rows: list[dict] = []

    for campaign_id in campaign_ids:
        try:
            # Campaign details: name, status, settings (changes rarely — no local cache needed)
            details = bison.get_campaign_details(campaign_id)
            start_date = _get_start_date(supabase, campaign_id, details)

            # Time-series chart stats
            raw_stats = bison.get_campaign_line_area_chart_stats(
                campaign_id, start_date, today
            )
            by_date = _parse_chart_stats(raw_stats)
            if not by_date:
                logger.warning(f"No chart stats returned for campaign {campaign_id}")
                continue

            fetched_at = datetime.now(timezone.utc).isoformat()
            for date_str, metrics in by_date.items():
                sent = metrics.get("emails_sent", 0)
                unique_opens = metrics.get("unique_opens", 0)
                replied = metrics.get("emails_replied", 0)
                bounced = metrics.get("emails_bounced", 0)

                all_rows.append({
                    "workspace_id": workspace_id,
                    "campaign_id": str(campaign_id),
                    "campaign_name": details.get("name") or details.get("campaign_name"),
                    "campaign_status": details.get("status"),
                    "stat_date": date_str,
                    "emails_sent": sent,
                    "emails_opened": metrics.get("emails_opened", 0),
                    "unique_opens": unique_opens,
                    "emails_replied": replied,
                    "emails_bounced": bounced,
                    "unsubscribed": metrics.get("unsubscribed", 0),
                    "interested": metrics.get("interested", 0),
                    "open_rate": _safe_rate(unique_opens, sent),
                    "reply_rate": _safe_rate(replied, sent),
                    "bounce_rate": _safe_rate(bounced, sent),
                    "max_emails_per_day": details.get("max_emails_per_day"),
                    "max_new_leads_per_day": details.get("max_new_leads_per_day"),
                    "plain_text": details.get("plain_text"),
                    "open_tracking": details.get("open_tracking"),
                    "sequence_prioritization": details.get("sequence_prioritization"),
                    "fetched_at": fetched_at,
                })
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.warning(f"Campaign {campaign_id} not found (404) — may have been deleted from EmailBison")
            else:
                logger.error(f"Failed to process daily stats for campaign {campaign_id}: {e}")
        except Exception as e:
            logger.error(f"Failed to process daily stats for campaign {campaign_id}: {e}")

    if all_rows:
        try:
            supabase.table("campaign_daily_stats").upsert(
                all_rows, on_conflict="workspace_id,campaign_id,stat_date"
            ).execute()
            logger.info(f"[{workspace_id}] Batch-upserted {len(all_rows)} campaign daily stat rows")
        except Exception as e:
            logger.error(f"Failed to batch-upsert campaign daily stats: {e}")


def run() -> None:
    """Main entry point called by the scheduler."""
    logger.info("Starting campaign daily stats poll")
    for ws in pollable_workspaces():
        try:
            bison = emailbison.for_workspace(ws["id"])
        except Exception as e:
            logger.error(f"Skipping workspace {ws['id']}: {e}")
            continue
        try:
            poll_campaign_daily_stats(ws["id"], bison)
        except Exception as e:
            logger.error(
                f"campaign_daily_stats_poller failed for workspace {ws['id']}: {e}"
            )
    logger.info("Campaign daily stats poll complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
