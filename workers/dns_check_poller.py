"""
dns_check_poller.py — runs every 12 hours

For every sender domain, performs live DNS TXT lookups and writes one
dns_health_checks row per domain per run capturing:
  - SPF pass/fail + record
  - DKIM pass/fail + selector that matched
  - DMARC pass/fail + policy
"""

import logging
from datetime import datetime, timezone

import dns.resolver
import dns.exception

from lib import emailbison
from lib.supabase_client import get_supabase

logger = logging.getLogger(__name__)

_DKIM_SELECTORS = ("default", "google", "selector1", "selector2", "k1", "mail", "dkim")
_RESOLVER = dns.resolver.Resolver()
_RESOLVER.lifetime = 5
_RESOLVER.timeout = 5


def _txt_records(name: str) -> list[str]:
    try:
        answers = _RESOLVER.resolve(name, "TXT")
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.resolver.NoNameservers, dns.exception.Timeout):
        return []
    except Exception as e:
        logger.debug(f"TXT lookup failed for {name}: {e}")
        return []
    records: list[str] = []
    for r in answers:
        parts = [p.decode() if isinstance(p, bytes) else str(p) for p in getattr(r, "strings", [])]
        records.append("".join(parts) if parts else r.to_text().strip('"'))
    return records


def _check_spf(domain: str) -> tuple[bool | None, str | None]:
    for record in _txt_records(domain):
        if record.lower().startswith("v=spf1"):
            passed = " -all" in record.lower() or " ~all" in record.lower() or record.lower().endswith("-all") or record.lower().endswith("~all")
            return passed, record
    return False, None


def _check_dkim(domain: str) -> tuple[bool | None, str | None]:
    for selector in _DKIM_SELECTORS:
        records = _txt_records(f"{selector}._domainkey.{domain}")
        for record in records:
            if "v=dkim1" in record.lower() or "p=" in record.lower():
                return True, selector
    return False, None


def _check_dmarc(domain: str) -> tuple[bool | None, str | None]:
    for record in _txt_records(f"_dmarc.{domain}"):
        if record.lower().startswith("v=dmarc1"):
            policy = "none"
            for part in record.split(";"):
                part = part.strip().lower()
                if part.startswith("p="):
                    policy = part[2:].strip()
                    break
            passed = policy in ("quarantine", "reject")
            return passed, policy
    return False, None


def _domains() -> list[str]:
    try:
        senders = emailbison.get_sender_emails()
    except Exception as e:
        logger.error(f"Failed to fetch sender emails from EmailBison: {e}")
        return []
    seen: set[str] = set()
    out: list[str] = []
    for s in senders:
        email = s.get("email") or ""
        if "@" in email:
            domain = email.split("@", 1)[1].lower()
            if domain and domain not in seen:
                seen.add(domain)
                out.append(domain)
    return out


def poll_dns_health() -> None:
    supabase = get_supabase()
    domains = _domains()
    logger.info(f"Checking DNS for {len(domains)} domains")
    if not domains:
        return

    checked_at = datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []
    for domain in domains:
        spf_passed, spf_record = _check_spf(domain)
        dkim_passed, dkim_selector = _check_dkim(domain)
        dmarc_passed, dmarc_policy = _check_dmarc(domain)
        rows.append({
            "domain": domain,
            "spf_passed": spf_passed,
            "spf_record": spf_record,
            "dkim_passed": dkim_passed,
            "dkim_selector": dkim_selector,
            "dmarc_passed": dmarc_passed,
            "dmarc_policy": dmarc_policy,
            "checked_at": checked_at,
        })

    try:
        supabase.table("dns_health_checks").insert(rows).execute()
        logger.info(f"Inserted {len(rows)} DNS health rows")
    except Exception as e:
        logger.error(f"Failed to insert dns_health_checks: {e}")


def run() -> None:
    logger.info("Starting DNS health poll")
    try:
        poll_dns_health()
    except Exception as e:
        logger.error(f"dns_check_poller.poll_dns_health failed: {e}")
    logger.info("DNS health poll complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
