"""Publisher identity and claim independence.

Two gates in this system count "independent sources": the two-domain city
admission rule, and the corroboration breadth that multiplies social quality.
Both counted **raw `source_domain` strings**, and that turns out to be two
distinct mistakes stacked on each other.

**The strings were not normalised consistently.** `apidirect._domain()`
lowercases an explicit vendor value and returns immediately; `www.` is stripped
only on the URL-parsing fallback. So `www.apnews.com` and `apnews.com` counted
as two independent publishers. Worse, for news the fallback key is
`source_name`, so a display name — `"associated press"` — was stored in the
domain column and counted as a domain.

**Normalising hostnames would not have been enough.** Two hosts carrying the
same wire story are not two claims. Triage deduplicates by exact URL, so
syndicated copies at different URLs counted as independent corroboration of each
other. Corroboration is supposed to mean two people looked and agreed; forty
reprints of one dispatch is one person looking.

So there are two identities here, and they answer different questions:

    publisher_key   who is telling us          (admission: two publishers)
    claim_key       what they are telling us   (breadth: two distinct claims)

`services/geo.py` is the model for the resolver, and its docstring is a
post-mortem of exactly this class of bug — a bidirectional prefix match that
returned a confident answer for the wrong city. The discipline it settled on is
reproduced here: a canonical table, an explicit alias map, a minimum length
before any fuzzy step, a uniqueness requirement, and a closed `(key, method)`
vocabulary so the decision is auditable.

**Unknown provenance is UNKNOWN, never automatically independent.** A post whose
publisher cannot be established gets a key derived from nothing but itself, and
`independent_publishers()` refuses to count two such posts as two publishers.
That is the conservative direction: under-counting corroboration delays an
alert, over-counting manufactures one.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlparse, urlunparse

#: Bumped when the table or the rules below change. Stamped on every signal and
#: carried on every receipt as `normaliser_version`, so a corroboration decision
#: can be reconstructed under the rules that made it — and so two runs that
#: counted corroboration differently cannot look identical afterwards.
#:
#: /2 — claim identity became content-first (see `claim_of`).
RULES_VERSION = "provenance/2"

#: Below this a host fragment is too short to resolve by anything but an exact
#: match. Mirrors geo.MIN_PREFIX_LEN and exists for the same reason.
MIN_HOST_LEN = 4

#: Multi-part public suffixes we actually encounter. Not a full PSL — a
#: dependency and a periodic update for a handful of hosts is a poor trade, and
#: a missing entry fails in the safe direction: `bbc.co.uk` would resolve to
#: `co.uk` and merge two BBC hosts that really are one publisher anyway.
COMPOUND_SUFFIXES: frozenset[str] = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "co.jp", "com.au", "gov.au",
    "co.nz", "com.br", "co.za", "com.mx",
})

#: An alias is a deliberate statement that two strings name the same publisher.
#: A prefix coincidence is not. Display names appear here because API Direct's
#: news endpoint puts them in the domain field when it has no host.
PUBLISHER_ALIASES: dict[str, str] = {
    # Wire services and their display names
    "associated press": "apnews.com",
    "the associated press": "apnews.com",
    "ap news": "apnews.com",
    "ap": "apnews.com",
    "reuters": "reuters.com",
    "thomson reuters": "reuters.com",
    "agence france-presse": "afp.com",
    "afp": "afp.com",
    "bloomberg": "bloomberg.com",
    "bloomberg news": "bloomberg.com",
    # National outlets
    "the new york times": "nytimes.com",
    "new york times": "nytimes.com",
    "nyt": "nytimes.com",
    "the washington post": "washingtonpost.com",
    "washington post": "washingtonpost.com",
    "the wall street journal": "wsj.com",
    "wall street journal": "wsj.com",
    "usa today": "usatoday.com",
    "npr": "npr.org",
    "national public radio": "npr.org",
    "pbs": "pbs.org",
    "pbs newshour": "pbs.org",
    "cnn": "cnn.com",
    "fox news": "foxnews.com",
    "nbc news": "nbcnews.com",
    "cbs news": "cbsnews.com",
    "abc news": "abcnews.go.com",
    "politico": "politico.com",
    "axios": "axios.com",
    "the hill": "thehill.com",
    "the guardian": "theguardian.com",
    "guardian": "theguardian.com",
    "bbc": "bbc.com",
    "bbc news": "bbc.com",
    # Regional outlets are a MISSION's business, not the engine's: which local
    # paper is the record of a jurisdiction depends entirely on which
    # jurisdictions you watch. A mission adds them through
    # `geography.yaml: publishers`, and they are merged on top of this table.
    # Platforms, so a platform fallback resolves consistently
    "twitter": "twitter.com",
    # Stored in NORMALISED form. "twitter (x)" would be a dead entry: the
    # lookup strips parentheses, so that key could never be reached.
    "twitter x": "twitter.com",
    "x": "twitter.com",
    "x twitter": "twitter.com",
    "reddit": "reddit.com",
    "facebook": "facebook.com",
    "bluesky": "bsky.app",
    "mastodon": "mastodon.social",
    "truth social": "truthsocial.com",
    "telegram": "telegram.org",
}

#: Host prefixes that are delivery mechanics, not distinct publishers.
_STRIP_LABELS: tuple[str, ...] = (
    "www.", "www2.", "m.", "mobile.", "amp.", "edition.", "web.",
)

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s.\-]")

#: Tracking parameters, removed before a URL becomes a claim identity. Two links
#: to one article that differ only by campaign tag are one claim.
_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "mc_", "ref", "source",
                      "igshid", "s", "t", "cmpid", "ito")


@dataclass(frozen=True)
class Publisher:
    """Who told us, and how confidently we know that."""

    key: str
    #: TABLE, ALIAS, HOST, PLATFORM or UNKNOWN — the same closed-vocabulary
    #: discipline as geo_cache.resolved_by.
    method: str

    @property
    def known(self) -> bool:
        return self.method != "UNKNOWN"


def normalise_name(value: Any) -> str:
    """Lowercase, punctuation-stripped, whitespace-collapsed."""
    text = str(value or "").strip().lower()
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def registrable_domain(host: str) -> str:
    """The part of a hostname that identifies an organisation.

    `news.bbc.co.uk` and `www.bbc.co.uk` are one publisher; `substack.com`
    subdomains are not, but that is a distinction this system does not need to
    make and would get wrong more often than right.
    """
    host = host.strip().lower().rstrip(".")
    if not host:
        return ""
    for label in _STRIP_LABELS:
        if host.startswith(label):
            host = host[len(label):]
            break
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in COMPOUND_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def resolve_publisher(
    domain: Any = None, url: Any = None, platform: Any = None,
    extra: Mapping[str, str] | None = None,
) -> Publisher:
    """Resolve a post's source to a canonical publisher key.

    Layered, most reliable first, and it refuses rather than guessing:

      ALIAS     an explicit statement that this string names this publisher
      HOST      a value that parses as a hostname, reduced to its registrable
                domain
      PLATFORM  no publisher, but a known platform — twitter.com is a real
                answer for a tweet
      UNKNOWN   nothing usable. Callers must not treat two of these as two
                publishers.
    """
    for candidate in (domain, url, platform):
        resolved = _resolve_one(candidate, extra)
        if resolved is not None:
            return resolved
    return Publisher("", "UNKNOWN")


def _resolve_one(value: Any,
                 extra: Mapping[str, str] | None = None) -> Publisher | None:
    aliases = {**PUBLISHER_ALIASES, **(extra or {})}
    text = str(value or "").strip()
    if not text:
        return None

    alias = aliases.get(normalise_name(text))
    if alias:
        return Publisher(alias, "ALIAS")

    host = text.lower()
    if "://" in host or host.startswith("//"):
        try:
            host = urlparse(text if "://" in text else f"https:{text}").netloc
        except ValueError:
            return None
    host = host.split("/")[0].split("@")[-1].split(":")[0]

    if "." in host and len(host) >= MIN_HOST_LEN:
        key = registrable_domain(host)
        if key:
            alias = aliases.get(key)
            return Publisher(alias or key, "ALIAS" if alias else "HOST")
    return None


def publisher_of(post: Mapping[str, Any],
                 mission: Any = None) -> Publisher:
    """Resolve from a normalised post record, in field-preference order.

    `mission` supplies the regional outlets this deployment depends on, merged
    ON TOP of the engine's wire services and nationals. Without one, a regional
    domain still resolves — as a HOST rather than an ALIAS, which is the honest
    answer: the engine knows the domain and not the masthead behind it.
    """
    return resolve_publisher(
        domain=post.get("source_domain"),
        url=post.get("url"),
        platform=post.get("platform"),
        extra=getattr(mission, "publishers", None),
    )


def canonical_url(url: Any) -> str:
    """A URL reduced to what identifies the document.

    Scheme and host case, `www.`, tracking parameters and fragments are all
    delivery mechanics. Two links differing only in those are one claim.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parts = urlparse(text)
    except ValueError:
        return text.lower()
    if not parts.netloc:
        return text.lower()
    host = parts.netloc.lower()
    for label in _STRIP_LABELS:
        if host.startswith(label):
            host = host[len(label):]
            break
    query = "&".join(sorted(
        chunk for chunk in parts.query.split("&")
        if chunk and not chunk.split("=")[0].lower().startswith(
            _TRACKING_PREFIXES)
    ))
    path = parts.path.rstrip("/") or "/"
    return urlunparse(("https", host, path, "", query, "")).lower()


#: Words of normalised text before a fingerprint may merge two posts.
#:
#: The gate is on SPECIFICITY, not on length for its own sake. A short post
#: shares its opening words with many others, so merging on them would collapse
#: genuinely separate reports into one claim and UNDER-count corroboration in a
#: way nothing downstream could see. A wire paragraph does not.
_MIN_FINGERPRINT_WORDS = 12


def claim_of(post: Mapping[str, Any]) -> str:
    """A conservative identity for *what is being said*.

    **Content first, URL second.** A wire story reprinted at two addresses
    under two mastheads is one claim, and in production both copies carry a
    real URL — triage refuses a post without one — so preferring the URL made
    the syndication clause unreachable for exactly the traffic it was written
    for. Two publishers running the same paragraph then satisfied the
    independence gate, and republication breadth read as independent
    reporting: enough, with `expand_cities`, to admit a city on one story.

    The URL still identifies a document. Two links to one article, differing
    only by `www.` or tracking parameters, are one claim by that route, and it
    is the route taken whenever the text is too thin to be specific.

    Conservative on purpose in both directions: the fingerprint is over
    normalised words, so trivial edits break it and two genuinely distinct
    reports never merge; and the word floor keeps short posts on URL identity.
    It under-detects syndication rather than over-detecting it, and the cost of
    that direction is a delayed alert rather than a manufactured one.
    """
    text = normalise_name(
        f"{post.get('title') or ''} {post.get('snippet') or ''}"
    )
    words = text.split()
    if len(words) >= _MIN_FINGERPRINT_WORDS:
        return f"t:{hashlib.sha256(' '.join(words[:40]).encode()).hexdigest()[:16]}"
    canonical = canonical_url(post.get("url"))
    if canonical:
        return f"u:{hashlib.sha256(canonical.encode()).hexdigest()[:16]}"
    # Neither specific text nor an address. Fall back to an identity unique to
    # this post, so it is never merged with another.
    return f"x:{hashlib.sha256(repr(sorted(post.items())).encode()).hexdigest()[:16]}"


def publisher_for_row(row: Mapping[str, Any]) -> Publisher:
    """The stored publisher, or one resolved from the raw fields.

    The fallback is not a convenience. `publisher_key` is a nullable column
    added by migration, so every signal written before Phase 7 has NULL there —
    and treating those as unresolved would collapse the social quality of every
    historical correlation to a single unknown source.
    """
    key = row.get("publisher_key")
    if key:
        return Publisher(str(key), str(row.get("publisher_method") or "HOST"))
    return publisher_of(row)


def independent_publishers(rows: Iterable[Mapping[str, Any]]) -> int:
    """How many distinct, *known* publishers are represented.

    A row whose publisher could not be resolved contributes nothing. Two
    unknowns are not two publishers — that is the assumption that let `www.`
    prefixes and display names inflate the count in the first place.
    """
    keys = set()
    for row in rows:
        publisher = publisher_for_row(row)
        if publisher.known and publisher.key:
            keys.add(publisher.key)
    return len(keys)


def independent_claims(rows: Sequence[Mapping[str, Any]]) -> int:
    """How many distinct claims are represented.

    A row with no claim key counts as its own claim rather than merging with
    other unknowns — the same conservative direction: never assert that two
    things are the same report without evidence.
    """
    return corroboration(rows)[1]


def corroboration_weighted(
    rows: Sequence[Mapping[str, Any]],
    weight_of: "Callable[[Mapping[str, Any]], float]",
) -> tuple[float, float]:
    """`corroboration()` with each distinct publisher and claim counted at the
    weight of its FRESHEST observation (9.5).

    Freshest rather than summed, because the unit being counted is an
    independent source, not a report. One outlet that ran the same story eight
    times is still one publisher, and if temporal decay let repetition
    accumulate weight then an amplification campaign would score as
    corroboration — the precise confound `claim_key` exists to prevent.

    With every weight 1.0 this returns exactly `corroboration()`, which
    `tests/test_decay.py` asserts: the two share a definition of "independent"
    and must not drift apart.
    """
    publishers: dict[str, float] = {}
    claims: dict[str, float] = {}
    anonymous = 0.0
    for row in rows:
        weight = max(0.0, float(weight_of(row)))
        publisher = publisher_for_row(row)
        if publisher.known and publisher.key:
            publishers[publisher.key] = max(
                publishers.get(publisher.key, 0.0), weight)
        key = row.get("claim_key") or claim_of(row)
        if key:
            claims[key] = max(claims.get(key, 0.0), weight)
        else:
            # An unattributable row is its own claim, so it cannot merge with
            # another unknown and its weight is its own.
            anonymous += weight
    return sum(publishers.values()), sum(claims.values()) + anonymous


def corroboration(rows: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
    """(independent publishers, independent claims) for a set of social rows.

    Both are needed and they are not interchangeable. Two publishers reprinting
    one wire story is two publishers and one claim; one outlet running two
    separate investigations is one publisher and two claims. Corroboration
    requires both to be plural, and the gates take the *lower* of the two.
    """
    publishers = independent_publishers(rows)
    claims: set[str] = set()
    anonymous = 0
    for row in rows:
        # Same fallback as the publisher: a row written before Phase 7 has no
        # claim_key, and deriving one from its URL is exactly what would have
        # been stored.
        key = row.get("claim_key") or claim_of(row)
        if key:
            claims.add(key)
        else:
            anonymous += 1
    return publishers, len(claims) + anonymous
