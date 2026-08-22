"""Retention enforcement for licensed raw API payloads.

FlightRadar24's storage rules are explicit: "All data accumulated from the FR24
API should not be stored for more than 30 days from the date it was first
received. After this period, all stored data must be permanently deleted." The
rule applies to every FR24 endpoint uniformly.

This obligation exists because the database is file-backed. An in-memory
database would satisfy it for free, but iterations are separate API calls and
scheduled follow-ons have to survive between them, so the data lands on disk and
pruning becomes the code's job. Skipping it is a licence breach, not an
optimisation.

Design decision: purging a raw payload does NOT purge the signals derived from
it. The retention rule covers the licensed raw data, and the analytical record —
that an aircraft of a given category was inbound at a given time, and that it
contributed to an alert — is this system's own product. Signals keep a dangling
raw_id, and the evidence endpoint reports the payload as purged rather than
pretending it never existed.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from ..db.database import SurgeDB, utcnow
from . import governance

# Fallback when a provider has no configured retention. Deliberately the
# strictest of the four, so a missing config entry cannot cause a breach.
DEFAULT_RETENTION_DAYS = governance.DEFAULT_RETENTION_DAYS

_PROVIDER_CONFIG_SECTION: dict[str, str] = {
    "FR24": "flightradar",
    "APIDIRECT": "apidirect",
    "STAYING": "staying",
    "PRICELINE": "priceline",
}


def retention_days(config: Mapping[str, Any], provider: str) -> int:
    """Retention window for a provider, in days.

    The ceiling now comes from `services/governance.py` rather than from an
    `if provider == "FR24"` here (8.3). Same behaviour for FR24, but the limit
    now sits beside the licence text it comes from, and a provider that
    acquires a term later gets it enforced by adding one field rather than by
    remembering to add a branch.

    Config may SHORTEN a window and can never extend one past a contractual
    ceiling: a config file must not be able to buy a licence term.
    """
    section = _PROVIDER_CONFIG_SECTION.get(provider)
    configured = DEFAULT_RETENTION_DAYS
    if section:
        configured = int(
            (config.get(section) or {}).get(
                "retention_days", DEFAULT_RETENTION_DAYS
            )
        )
    return governance.retention_days(provider, configured)


class RetentionService:
    """Deletes raw payloads whose retention deadline has passed."""

    agent_name = "RetentionService"

    def __init__(self, db: SurgeDB, config: Mapping[str, Any]) -> None:
        self.db = db
        self.config = config

    def prune(self, now: datetime | None = None) -> int:
        """Delete expired raw_results and log the count. Returns rows deleted.

        Run at the end of every iteration and again at startup, so a process that
        sat idle past a deadline cleans up before it does anything else.
        """
        now = now or utcnow()
        pending = self.pending_report(now)
        deleted = self.db.purge_expired_raw(now)
        if deleted:
            self.db.log(
                self.agent_name, "INFO",
                f"Purged {deleted} raw payload(s) past their retention deadline",
                deleted=deleted, by_provider=pending,
            )
        return deleted

    def pending_report(self, now: datetime | None = None) -> dict[str, int]:
        """Count of expired-but-not-yet-purged payloads, by provider."""
        from ..db.database import iso

        rows = self.db.all(
            "SELECT provider, COUNT(*) AS n FROM raw_results "
            "WHERE purge_after <= ? GROUP BY provider",
            (iso(now or utcnow()),),
        )
        return {r["provider"]: int(r["n"]) for r in rows}

    def oldest_retained(self) -> dict[str, str]:
        """Earliest retrieved_at still held, by provider — a compliance check."""
        rows = self.db.all(
            "SELECT provider, MIN(retrieved_at) AS oldest FROM raw_results "
            "GROUP BY provider"
        )
        return {r["provider"]: r["oldest"] for r in rows if r["oldest"]}
