"""Shared config helpers for the Xero OAuth backend."""
from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

CALLBACK_SUFFIX = "/api/xero/callback"


def get_env(name: str) -> str:
    """Return environment value with surrounding whitespace/quotes removed."""
    raw = os.getenv(name, "")
    if not raw:
        return ""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value


def normalize_database_url(raw: str) -> str:
    """Return a Neon-compatible connection string (drop channel_binding, add endpoint)."""
    if not raw:
        return ""
    value = raw.strip().strip('"').strip("'")
    try:
        parts = urlsplit(value)
    except Exception:
        return value
    query_pairs = []
    has_endpoint_option = False
    for key, val in parse_qsl(parts.query, keep_blank_values=True):
        if key == "channel_binding":
            continue
        if key == "options" and "endpoint=" in val:
            has_endpoint_option = True
        query_pairs.append((key, val))
    if not has_endpoint_option:
        hostname = parts.hostname or ""
        endpoint = hostname.split(".")[0] if hostname else ""
        if endpoint:
            query_pairs.append(("options", f"endpoint={endpoint}"))
    new_query = urlencode(query_pairs, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def get_database_url() -> str:
    return normalize_database_url(get_env("DATABASE_URL"))

