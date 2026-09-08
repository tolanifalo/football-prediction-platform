"""Secret redaction and request signatures (P0-10 §15.7).

The nested/capitalised cases below are the exact gaps probed in the live
database: `raw_payloads`' CHECK constraints accept all of them, because
`jsonb_exists_any` tests top-level keys case-sensitively. These tests are the
real guard.
"""

from __future__ import annotations

import copy

from engine.ingestion.redaction import (
    REDACTED,
    normalise_headers,
    persistable_headers,
    scrub_params,
)
from engine.ingestion.signatures import body_hash, canonical_params, request_signature


class TestScrubParams:
    def test_top_level_api_key_is_redacted(self) -> None:
        assert scrub_params({"api_key": "SECRET"}) == {"api_key": REDACTED}

    def test_nested_api_key_is_redacted(self) -> None:
        """The database accepts this. We must not."""
        out = scrub_params({"query": {"api_key": "SECRET", "league": "E0"}})
        assert out == {"query": {"api_key": REDACTED, "league": "E0"}}

    def test_deeply_nested_api_key_is_redacted(self) -> None:
        out = scrub_params({"a": {"b": {"c": {"access_token": "SECRET"}}}})
        assert out["a"]["b"]["c"]["access_token"] == REDACTED

    def test_sequences_containing_mappings_are_scrubbed(self) -> None:
        out = scrub_params({"batch": [{"token": "SECRET"}, {"league": "E0"}]})
        assert out == {"batch": [{"token": REDACTED}, {"league": "E0"}]}

    def test_key_matching_is_case_and_separator_insensitive(self) -> None:
        out = scrub_params({"X-Api-Key": "S1", "API KEY": "S2", "Client_Secret": "S3"})
        assert set(out.values()) == {REDACTED}

    def test_clean_nested_structure_is_untouched(self) -> None:
        clean = {"league": "E0", "filters": {"season": "2526", "limit": 100}}
        assert scrub_params(clean) == clean

    def test_original_input_is_never_mutated(self) -> None:
        original = {"query": {"api_key": "SECRET", "items": [{"token": "T"}]}}
        snapshot = copy.deepcopy(original)
        scrub_params(original)
        assert original == snapshot, (
            "scrub_params must not mutate the caller's structure"
        )

    def test_strings_are_not_treated_as_sequences(self) -> None:
        assert scrub_params({"league": "E0"})["league"] == "E0"

    def test_free_text_values_are_deliberately_not_detected(self) -> None:
        """Documented limitation: we scrub keys, not arbitrary free text."""
        assert scrub_params({"note": "key=SECRET"}) == {"note": "key=SECRET"}


class TestHeaders:
    def test_names_are_lowercased(self) -> None:
        got = normalise_headers({"Content-Type": "text/csv"})
        assert got == {"content-type": "text/csv"}

    def test_capitalised_x_api_key_is_dropped(self) -> None:
        """The database accepts `X-Api-Key`. The whitelist must not."""
        assert persistable_headers({"X-Api-Key": "SECRET"}) == {}

    def test_capitalised_authorization_is_dropped(self) -> None:
        assert persistable_headers({"Authorization": "Bearer SECRET"}) == {}

    def test_set_cookie_is_dropped(self) -> None:
        assert persistable_headers({"Set-Cookie": "session=SECRET"}) == {}

    def test_whitelisted_headers_survive(self) -> None:
        out = persistable_headers(
            {"ETag": "abc", "Content-Type": "text/csv", "X-Session-Token": "SECRET"}
        )
        assert out == {"etag": "abc", "content-type": "text/csv"}

    def test_unknown_headers_are_dropped_not_kept(self) -> None:
        """A positive whitelist: an invented header has no provenance value."""
        assert persistable_headers({"X-Provider-Debug": "whatever"}) == {}


class TestRequestSignature:
    def test_is_deterministic_regardless_of_key_order(self) -> None:
        a = request_signature("/v3/fixtures", {"league": "E0", "season": "2526"})
        b = request_signature("/v3/fixtures", {"season": "2526", "league": "E0"})
        assert a == b

    def test_credentials_are_excluded_entirely(self) -> None:
        """Two developers with different keys must sign identically."""
        plain = request_signature("/v3/fixtures", {"league": "E0"})
        keyed = request_signature("/v3/fixtures", {"league": "E0", "api_key": "SECRET"})
        other = request_signature(
            "/v3/fixtures",
            {"league": "E0", "api_key": "DIFFERENT"},
        )
        assert plain == keyed == other

    def test_credentials_are_removed_not_redacted(self) -> None:
        assert canonical_params({"api_key": "S", "league": "E0"}) == {"league": "E0"}

    def test_a_meaningful_parameter_change_changes_the_signature(self) -> None:
        a = request_signature("/v3/fixtures", {"league": "E0"})
        b = request_signature("/v3/fixtures", {"league": "E1"})
        assert a != b

    def test_a_different_endpoint_changes_the_signature(self) -> None:
        assert request_signature("/v3/fixtures") != request_signature("/v3/results")

    def test_is_sha256_hex(self) -> None:
        sig = request_signature("/v3/fixtures")
        assert len(sig) == 64 and set(sig) <= set("0123456789abcdef")


class TestBodyHash:
    def test_hashes_exact_bytes(self) -> None:
        # sha256 of the empty string - the value a 429 with no body produces.
        assert body_hash(b"") == (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )

    def test_identical_bytes_hash_identically(self) -> None:
        assert body_hash(b'{"a":1}') == body_hash(b'{"a":1}')

    def test_json_is_not_canonicalised_before_hashing(self) -> None:
        """Semantically equal, textually different -> different hashes (§9.4)."""
        assert body_hash(b'{"a":1,"b":2}') != body_hash(b'{"b":2,"a":1}')
