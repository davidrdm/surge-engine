"""Connector construction, including the fixture-backed dry-run mode.

One place builds connectors, so credentials are read exactly once, rate limiters
are shared per provider rather than per call site, and every connector is wired
to the budget ledger without a call site having to remember.

`dry_run: true` swaps the HTTP transport for one that serves recorded fixtures.
The real connector classes, parsers and validators still run — only the network
is replaced. That matters: a dry run that stubbed out the connectors would prove
nothing about the parsing, and this way the front end develops against the same
code paths production uses.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

from ..base.connector import BaseConnector
from ..config import require_api_key
from ..services.ratelimit import RateLimiter, build_limiters
from .apidirect import APIDirectConnector
from .flightradar import FlightRadarConnector
from .priceline import PricelineConnector
from .staying import StayingConnector

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"

# Path -> fixture file, for dry-run mode.
_DRY_RUN_FIXTURES: dict[str, str] = {
    "/v1/twitter/posts": "apidirect_twitter.json",
    "/v1/reddit/posts": "apidirect_twitter.json",
    "/v1/news/articles": "apidirect_news.json",
    "/api/live/flight-positions/count": "fr24_live_count.json",
    "/api/live/flight-positions/full": "fr24_live_positions.json",
    "/api/flight-summary/full": "fr24_flight_summary.json",
    "/account": "staying_account.json",
    "/search": "staying_search.json",
    "/availability": "staying_availability_near.json",
    # The CURRENT wrapper's shape. dry_run is what a front end develops
    # against and what a demo runs on, so serving the superseded priceline8
    # payload here would have every consumer built against a shape the system
    # no longer receives.
    "/cars/search": "priceline_com2_cars.json",
    "/cars/auto-complete": "priceline_autocomplete.json",
    # Legacy paths still resolve, and deliberately still serve the legacy
    # payload — a pre-8.5 queue row replayed in dry_run should look like what
    # it actually collected.
    "/search-rental-car": "priceline_cars.json",
    "/auto-complete-location": "priceline_autocomplete.json",
}


class DryRunTransport(httpx.BaseTransport):
    """Serves recorded fixtures instead of making network calls.

    Deliberately returns 404 for an unmapped path rather than an empty success.
    A dry run that answered every request with "nothing found" would mask a
    wiring mistake as a quiet lack of intelligence, which is the same failure
    mode the connectors exist to prevent.
    """

    def __init__(self, fixture_dir: Path = FIXTURE_DIR) -> None:
        self.fixture_dir = fixture_dir
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        name = _DRY_RUN_FIXTURES.get(path)
        if name is None and path.startswith("/v1/"):
            # Staying's base URL carries a /v1 that its OpenAPI paths omit.
            name = _DRY_RUN_FIXTURES.get(path[3:])
            path = path[3:]
        if name is None and path.startswith("/jobs/"):
            name = "staying_job_complete.json"
        if name is None:
            return httpx.Response(
                404, json={"message": f"dry-run: no fixture mapped for {path}"},
                request=request,
            )
        payload = json.loads((self.fixture_dir / name).read_text())
        return httpx.Response(200, json=payload, request=request)


def build_connectors(
    config: Mapping[str, Any],
    *,
    on_call: Callable[..., Any] | None = None,
    limiters: Mapping[str, RateLimiter] | None = None,
) -> dict[str, BaseConnector]:
    """Construct every connector, keyed by provider code.

    In dry-run mode the credentials are not read at all, so a demo works with an
    empty .env.
    """
    dry_run = bool(config.get("dry_run"))
    # No pacing against fixtures. The limiter exists to keep four vendors from
    # returning 429, and in dry-run there is no vendor — FR24's measured ceiling
    # of one request every six seconds would otherwise make a demo iteration
    # take minutes to read files off the local disk.
    limiters = limiters or ({} if dry_run else build_limiters(config))

    def client() -> httpx.Client | None:
        return httpx.Client(transport=DryRunTransport()) if dry_run else None

    def key(section: str, placeholder: str) -> str:
        return placeholder if dry_run else require_api_key(config, section)

    fr_cfg = dict(config.get("flightradar") or {})
    # Sandbox and production share the base URL and paths; only the key differs.
    # A sandbox key returns static responses that ignore query parameters, so it
    # proves parsing and auth but cannot prove a filter is correct.
    fr_key_section = "flightradar"
    fr_key = key(fr_key_section, "dry-run")
    if not dry_run and fr_cfg.get("sandbox"):
        import os
        sandbox_key = os.environ.get("FR24_SANDBOX_KEY", "")
        if not sandbox_key:
            raise RuntimeError(
                "flightradar.sandbox is true but FR24_SANDBOX_KEY is not set"
            )
        fr_key = sandbox_key

    staying_cfg = dict(config.get("staying") or {})
    priceline_cfg = dict(config.get("priceline") or {})

    return {
        "APIDIRECT": APIDirectConnector(
            key("apidirect", "dry-run"),
            base_url=(config.get("apidirect") or {}).get(
                "base_url", "https://apidirect.io"),
            limiter=limiters.get("APIDIRECT"), on_call=on_call, client=client(),
        ),
        "FR24": FlightRadarConnector(
            fr_key,
            base_url=fr_cfg.get("base_url", "https://fr24api.flightradar24.com"),
            limiter=limiters.get("FR24"), on_call=on_call, client=client(),
        ),
        "STAYING": StayingConnector(
            key("staying", "stay_test_dry-run"),
            base_url=staying_cfg.get("base_url", "https://api.stayingapi.com"),
            poll_max_s=float(staying_cfg.get("job_poll_max_s", 120)),
            limiter=limiters.get("STAYING"), on_call=on_call, client=client(),
        ),
        "PRICELINE": PricelineConnector(
            key("priceline", "dry-run"),
            host=priceline_cfg.get("host", "priceline-com2.p.rapidapi.com"),
            limiter=limiters.get("PRICELINE"), on_call=on_call, client=client(),
        ),
    }


def health_report(connectors: Mapping[str, BaseConnector]) -> dict[str, Any]:
    """Per-connector health, for GET /healthz.

    Each connector reports its own failure rather than raising, because an
    operator checking health needs to see which of the four is broken, not just
    that something is.

    The try/except is not defensive clutter: a connector whose health check
    itself throws — a transport error, a substituted stub, a vendor returning
    something unparseable — would otherwise turn the endpoint that exists to
    report failures into one, and an operator would see a 500 with no idea which
    provider caused it.
    """
    report: dict[str, Any] = {}
    for name, connector in sorted(connectors.items()):
        try:
            report[name] = connector.health_check()
        except Exception as exc:  # noqa: BLE001 — the report IS the error path
            report[name] = {"provider": name, "healthy": False,
                            "detail": f"{type(exc).__name__}: {exc}"}
    return report
