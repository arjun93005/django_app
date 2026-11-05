from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def canonicalize_url(url: str, base: str | None = None) -> str:
    """Normalise a URL so the same page always hashes to the same frontier key.

    Resolves relative links (``../../catalogue/x/index.html``), lowercases the
    scheme/host, drops default ports and fragments, and sorts query params.
    """
    if base:
        url = urljoin(base, url)
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if parts.port and parts.port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{parts.port}"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((scheme, host, parts.path or "/", query, ""))
