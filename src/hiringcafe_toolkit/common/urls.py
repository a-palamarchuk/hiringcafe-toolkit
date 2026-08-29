"""Host normalization.

Company identity across this toolkit is the normalized website host. The same
normalization is used for the VisitLogger export keys, so the visited-filter
stage can compare them directly.
"""

from __future__ import annotations

from urllib.parse import urlsplit

WWW_PREFIX = "www."


def normalize_host(value: str | None) -> str | None:
    """Reduce a URL or bare hostname to a lowercase host without ``www.``.

    Accepts what the data actually contains: bare hosts (``ionq.com``), full
    URLs, hosts with ports or trailing paths, and values with stray whitespace.
    Returns ``None`` for anything that does not yield a plausible host.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None

    if "//" not in text:
        text = "//" + text
    host = urlsplit(text).hostname
    if not host:
        return None

    host = host.strip(".").lower()
    if host.startswith(WWW_PREFIX):
        host = host[len(WWW_PREFIX) :]
    # A host with no dot is not a usable public site (localhost, a typo, or a
    # bare token); treating it as company identity would merge unrelated rows.
    if "." not in host:
        return None
    return host


def host_to_url(host: str) -> str:
    """Render a normalized host as a link target."""
    return f"https://{host}"


#: Prefix marking a VisitLogger key as a job posting rather than a company host.
#:
#: The extension exports all of local storage as one flat object, so both
#: pipelines' keys share a file. The prefix keeps them apart without changing
#: the extension: it contains no dot, so host normalization rejects it, and it
#: cannot collide with an ATS domain.
POSTING_KEY_PREFIX = "job:"


def posting_key(object_id: str) -> str:
    """The VisitLogger key for a posting.

    Not passed through ``normalize_host``: that lower-cases, and posting ids
    are case-sensitive, so normalizing would silently break every lookup.
    """
    return f"{POSTING_KEY_PREFIX}{object_id}"


def host_covers(owner: str, candidate: str) -> bool:
    """Whether ``candidate`` belongs to the employer that owns ``owner``.

    True for the host itself and for any subdomain of it. Needed because the
    visit log records whatever host was actually opened, which is often a
    careers subdomain - ``careers.appian.com``, ``careers.confluent.io`` - while
    the pipeline stores the registrable domain from the company's homepage. An
    exact comparison silently misses those, and the miss looks like "no resume
    sent here" rather than like a bug.
    """
    owner = normalize_host(owner) or ""
    candidate = normalize_host(candidate) or ""
    if not owner or not candidate:
        return False
    return candidate == owner or candidate.endswith(f".{owner}")


def applied_to(host: str | None, applied_hosts: frozenset[str]) -> bool:
    """Whether a resume has gone to this employer, under any of its hosts."""
    if not host:
        return False
    return any(host_covers(host, candidate) for candidate in applied_hosts)
