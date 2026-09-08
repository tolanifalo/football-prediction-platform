"""Request signatures and body hashing (PHASE-0-SPEC.md §9.4, §15.7).

Two hashes with two different jobs:

  request_signature  identifies the REQUEST, so the same logical call made by
                     two people with two API keys signs identically.
  body_hash          identifies the RESPONSE CONTENT, and is the key that lets
                     `raw_payload_bodies` deduplicate globally.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from engine.ingestion.redaction import is_sensitive_name


def canonical_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Drop credentials, then sort. Credentials are removed, not redacted.

    Removed rather than replaced with a marker because the signature must be
    stable across callers: two developers with different keys must produce the
    same signature for the same logical request.
    """
    kept = {k: v for k, v in sorted(params.items()) if not is_sensitive_name(k)}
    return kept


def request_signature(endpoint: str, params: Mapping[str, Any] | None = None) -> str:
    """SHA-256 over the canonical endpoint plus its non-credential parameters.

    `endpoint` is the template - "/v3/fixtures" - never the interpolated URL,
    so a signature never contains a query string with a key in it (§9.1).
    """
    payload = {
        "endpoint": endpoint.strip(),
        "params": canonical_params(params or {}),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def body_hash(content: bytes) -> str:
    """SHA-256 of the EXACT decompressed response bytes (§9.4).

    After transport decoding (gzip undone) and BEFORE any parsing. JSON is
    never canonicalised first: a canonicaliser's output can change with a
    library upgrade and silently invalidate every historical hash. Determinism
    across time beats deduplication efficiency.
    """
    return hashlib.sha256(content).hexdigest()
