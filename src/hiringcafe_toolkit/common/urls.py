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
