"""Social media and news collection via API Direct (apidirect.io).

Adapted from surge/connectors/apidirect.py with two corrections.

1. The news path was `/v1/news`. The real path is `/v1/news/articles`; the old
   one would 404, and because the old connector swallowed HTTPError and returned
   [], every news sweep silently produced nothing.

2. The old connector sent `{query, pages, sort_by}` to every endpoint. The
   endpoints do not share a parameter vocabulary:

     /v1/twitter/posts   query, pages (1-10), sort_by, get_sentiment
                         -> {posts: [...], pages, count}     NO time filter
     /v1/news/articles   query, limit (1-100), time_published, source,
                         country, language
                         -> {articles: [...], limit, count}

   `time_published` is the only real recency control this provider offers, and
   the 48-hour tipping window depends on it. Sending `pages` to the news
   endpoint gets it ignored and the default limit of 10 applied instead.

No sandbox environment exists here, so tests run against recorded fixtures and
the live smoke step costs a few cents.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from ..base.connector import BaseConnector, SchemaError

EP_TWITTER = "/v1/twitter/posts"
EP_REDDIT = "/v1/reddit/posts"
EP_NEWS = "/v1/news/articles"

PLATFORM_ENDPOINTS: dict[str, str] = {
    "twitter": EP_TWITTER,
    "reddit": EP_REDDIT,
    "news": EP_NEWS,
}

# Which envelope key holds the records, per endpoint. Chosen explicitly rather
# than by the old `posts or articles or results` fallback chain, which would
# silently accept a differently-shaped response as empty.
_ENVELOPE: dict[str, str] = {
    EP_TWITTER: "posts",
    EP_REDDIT: "posts",
    EP_NEWS: "articles",
}


class APIDirectConnector(BaseConnector):
    """Searches social platforms and news. Fails loud on any HTTP error."""

    provider = "APIDIRECT"

    def __init__(self, api_key: str, *, base_url: str = "https://apidirect.io",
                 **kwargs: Any) -> None:
        super().__init__(api_key, base_url=base_url, **kwargs)

    @property
    def name(self) -> str:
        return "API Direct"

    def auth_headers(self) -> dict[str, str]:
        # Header only, never a query string: query strings land in provider
        # access logs and in any error message that echoes the URL.
        return {"X-API-Key": self._api_key}

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        endpoint: str,
        params: Mapping[str, Any],
        *,
        iteration_id: int | None = None,
        query_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Execute one prepared query and return normalised posts.

        `params` arrives already shaped for the endpoint by QueueAgent, which
        owns the per-endpoint parameter differences.
        """
        if endpoint not in _ENVELOPE:
            raise SchemaError(
                f"Unknown API Direct endpoint {endpoint!r}",
                provider=self.provider, endpoint=endpoint,
            )
        key = _ENVELOPE[endpoint]
        response = self._request(
            endpoint,
            params=params,
            count_records=lambda data: len(_records(data, key)),
            iteration_id=iteration_id,
            query_id=query_id,
        )
        records = _records(response.data, key)
        normalise = _normalise_article if endpoint == EP_NEWS else _normalise_post
        platform = "news" if endpoint == EP_NEWS else (
            "reddit" if endpoint == EP_REDDIT else "twitter"
        )
        return [normalise(record, platform) for record in records]

    def health_check(self) -> dict[str, Any]:
        """Cheapest possible proof the key works: one page, one result.

        There is no free endpoint, so this costs a request. It is only called
        from /healthz, not per iteration.
        """
        try:
            self._request(
                EP_NEWS,
                params={"query": "city", "limit": 1,
                        "time_published": "1d"},
                count_records=lambda data: len(_records(data, "articles")),
            )
        except Exception as exc:  # noqa: BLE001 — reported, never swallowed
            return {"provider": self.provider, "healthy": False,
                    "detail": str(exc)}
        return {"provider": self.provider, "healthy": True, "detail": "ok"}


def _records(data: Any, key: str) -> list[dict[str, Any]]:
    """Unwrap the response envelope, insisting on the documented shape."""
    if not isinstance(data, dict):
        raise SchemaError(
            f"API Direct returned {type(data).__name__}, expected an object",
            provider="APIDIRECT",
        )
    records = data.get(key)
    if records is None:
        # An empty result is legitimate and must be distinguishable from a
        # shape change, so an absent envelope key is only tolerated when the
        # documented count field agrees that there were no results.
        if data.get("count") in (0, None) and not data.get(key):
            return []
        raise SchemaError(
            f"API Direct response has no {key!r} key; got {sorted(data)[:8]}",
            provider="APIDIRECT",
        )
    if not isinstance(records, list):
        raise SchemaError(
            f"API Direct {key!r} was {type(records).__name__}, expected a list",
            provider="APIDIRECT",
        )
    return records


def _domain(record: Mapping[str, Any], *keys: str) -> str:
    """Source domain, used as the independence key for corroboration.

    Corroboration is counted in distinct domains rather than post count, because
    forty reposts of one claim are one claim. Falls back to parsing the URL when
    the provider omits an explicit domain.
    """
    for key in keys:
        value = record.get(key)
        if value:
            return str(value).lower()
    url = record.get("url") or ""
    try:
        host = urlparse(str(url)).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _normalise_post(record: Mapping[str, Any], platform: str) -> dict[str, Any]:
    """Twitter/Reddit post -> the common social shape."""
    return {
        "url": record.get("url", ""),
        "title": record.get("title", ""),
        "author": record.get("author", ""),
        "platform": record.get("source") or platform,
        "source_domain": _domain(record, "domain"),
        "observed_at": record.get("date", ""),
        "snippet": str(record.get("snippet", ""))[:1000],
        "engagement": {
            "likes": record.get("likes"),
            "retweets": record.get("retweets"),
            "replies": record.get("replies"),
            "views": record.get("views"),
            "author_followers": record.get("author_followers"),
            "author_verified": record.get("author_verified"),
        },
        "hashtags": record.get("hashtags") or [],
        "lang": record.get("lang", ""),
    }


def _normalise_article(record: Mapping[str, Any], platform: str) -> dict[str, Any]:
    """News article -> the common social shape.

    Different field names entirely: `published_datetime_utc` rather than `date`,
    an `authors` list rather than an `author` string, and `source_name` for the
    outlet.
    """
    authors: Sequence[Any] = record.get("authors") or []
    return {
        "url": record.get("url", ""),
        "title": record.get("title", ""),
        "author": str(authors[0]) if authors else (record.get("source_name") or ""),
        "platform": platform,
        "source_domain": _domain(record, "domain", "source_name"),
        "observed_at": record.get("published_datetime_utc", ""),
        "snippet": str(record.get("snippet", ""))[:1000],
        "engagement": {},
        "hashtags": [],
        "lang": "",
    }
