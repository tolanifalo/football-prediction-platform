"""Credential scrubbing at the adapter boundary (PHASE-0-SPEC.md §15.7).

WHY THIS EXISTS RATHER THAN RELYING ON THE DATABASE. `raw_payloads` carries two
CHECK constraints against credentials, and they are defence in depth only:
`jsonb_exists_any` tests TOP-LEVEL keys, CASE-SENSITIVELY. Probed against the
live schema, the database accepts all of these:

    {"query": {"api_key": "SECRET"}}      nested          -> ACCEPTED
    {"X-Api-Key": "SECRET"}               capitalised     -> ACCEPTED
    {"Authorization": "Bearer ..."}       capitalised     -> ACCEPTED

HTTP header names are case-insensitive by specification and most clients
preserve the server's casing, so the whitelist would miss real credentials in
practice. This module is the real guard; the CHECKs stay as a backstop.

Scope, deliberately: we scrub KEYS, not free text. A credential pasted into a
value such as {"note": "key=SECRET"} is not detected, and pretending otherwise
would give false confidence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

#: Parameter names whose values are credentials. Compared case-insensitively
#: and after normalising '-' and ' ' to '_', so `Api-Key` and `API KEY` match.
SENSITIVE_PARAM_NAMES: Final[frozenset[str]] = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "access_token",
        "auth_token",
        "auth",
        "token",
        "password",
        "passwd",
        "pwd",
        "secret",
        "client_secret",
        "client_id",
        "private_key",
        "session",
        "cookie",
        "x_api_key",
        "x_auth_token",
        "key",
    }
)

#: Response headers worth keeping. POSITIVE whitelist: anything absent here is
#: dropped, so a provider inventing `X-Session-Token` never reaches the archive.
PERSISTED_HEADER_NAMES: Final[frozenset[str]] = frozenset(
    {
        "content-type",
        "content-length",
        "content-encoding",
        "date",
        "etag",
        "last-modified",
        "retry-after",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
    }
)

REDACTED: Final[str] = "[REDACTED]"


def _normalise(name: str) -> str:
    return name.strip().lower().replace("-", "_").replace(" ", "_")


def is_sensitive_name(name: str) -> bool:
    """True if a parameter or header of this name carries a credential."""
    return _normalise(name) in SENSITIVE_PARAM_NAMES


def scrub_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively replace credential values with a marker.

    Never mutates the input: nested mappings and sequences are rebuilt. That
    matters because the caller still needs the real values to send the request.
    """
    return _scrub_mapping(params)


def _scrub_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        if is_sensitive_name(key):
            out[key] = REDACTED
        else:
            out[key] = _scrub_value(value)
    return out


def _scrub_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _scrub_mapping(value)
    # str and bytes are Sequences; scrubbing them character-wise would be absurd.
    if isinstance(value, str | bytes | bytearray):
        return value
    if isinstance(value, Sequence):
        return [_scrub_value(item) for item in value]
    return value


def normalise_headers(
    headers: Iterable[tuple[str, str]] | Mapping[str, str],
) -> dict[str, str]:
    """Lowercase every header name. The first occurrence of a name wins.

    Header names are case-insensitive per RFC 9110, so lowercasing before any
    comparison is what makes the whitelist below actually work.
    """
    items = headers.items() if isinstance(headers, Mapping) else headers
    out: dict[str, str] = {}
    for name, value in items:
        lowered = name.strip().lower()
        if lowered not in out:
            out[lowered] = value
    return out


def persistable_headers(
    headers: Iterable[tuple[str, str]] | Mapping[str, str],
) -> dict[str, str]:
    """Lowercase, then keep only whitelisted names.

    This is what may be written to `raw_payloads.response_headers`. Everything
    else is discarded rather than redacted: an unrecognised header has no
    provenance value and might carry a credential.
    """
    lowered = normalise_headers(headers)
    return {
        name: value for name, value in lowered.items() if name in PERSISTED_HEADER_NAMES
    }
