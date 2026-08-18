"""Tests for the error taxonomy.

The endpoint answers HTTP 200 for every failure, so this classification IS
the error handling. Each payload below is a real response body captured
from the live API.
"""

import pytest

from app.services.youcloud.errors import (
    AuthExpired,
    BadFilter,
    PlanDenied,
    TransientError,
    classify,
    metric_label,
)


def _error(code=None, message=None, trace_id="trace-123"):
    extensions = {"traceId": trace_id}
    if code is not None:
        extensions["c"] = code
    if message is not None:
        extensions["m"] = message
    return {"errors": [{"message": None, "extensions": extensions}], "data": None}


class TestClassify:
    def test_success_payload_classifies_as_none(self):
        assert classify({"data": {"materialList": {"total": 1, "data": []}}}) is None

    def test_empty_errors_array_is_not_an_error(self):
        assert classify({"errors": [], "data": {"materialList": {}}}) is None

    def test_malformed_session_is_auth_expired(self):
        # Observed with a garbage sessionId cookie.
        exc = classify(_error("05:400001", "Login has expired. Please log in again."))
        assert isinstance(exc, AuthExpired)
        assert exc.code == "05:400001"

    def test_stale_session_is_auth_expired(self):
        # Observed once the real session's JWT exp passed. A DIFFERENT code
        # from the malformed case — which is why we branch on the `05:`
        # family rather than one exact value.
        exc = classify(_error("05:403001", "Login session has expired. Please log in again."))
        assert isinstance(exc, AuthExpired)
        assert exc.code == "05:403001"

    def test_unknown_session_family_code_still_auth_expired(self):
        exc = classify(_error("05:999999", "Some new session problem"))
        assert isinstance(exc, AuthExpired)

    def test_no_session_at_all_is_plan_denied(self):
        # Observed with no sessionId cookie: the platform reports it as a
        # plan problem, not an auth problem.
        exc = classify(_error("00:403001", "Permission denied, please upgrade your plan"))
        assert isinstance(exc, PlanDenied)

    def test_page_over_ceiling_is_bad_filter(self):
        # Observed at page=201. No distinguishing code — matched on message.
        exc = classify(_error(None, "Parameter error, please clear the filter and refresh"))
        assert isinstance(exc, BadFilter)

    def test_busy_message_is_transient(self):
        exc = classify(_error(None, "The system is busy, please try again later"))
        assert isinstance(exc, TransientError)

    def test_message_matching_is_case_insensitive(self):
        assert isinstance(classify(_error(None, "PARAMETER ERROR, clear the filter")), BadFilter)
        assert isinstance(classify(_error(None, "The System Is Busy")), TransientError)

    def test_unknown_error_defaults_to_transient(self):
        # Retryable-by-default: an unknown permanent error still exhausts
        # its attempts, whereas a terminal default would abandon a
        # recoverable blip we haven't catalogued.
        exc = classify(_error("77:123456", "Something entirely new"))
        assert isinstance(exc, TransientError)

    def test_falls_back_to_top_level_message(self):
        payload = {"errors": [{"message": "top level only"}], "data": None}
        exc = classify(payload)
        assert exc is not None and "top level only" in str(exc)

    def test_never_stringifies_none_as_the_message(self):
        # `message` is reliably null on this endpoint; a naive handler logs
        # the literal string "None".
        exc = classify({"errors": [{"message": None}], "data": None})
        assert exc is not None
        assert "None" not in str(exc)

    def test_trace_id_is_preserved(self):
        exc = classify(_error("05:403001", "expired", trace_id="abc123"))
        assert exc.trace_id == "abc123"

    def test_non_dict_payload_is_not_an_error(self):
        assert classify(None) is None
        assert classify("garbage") is None

    def test_str_includes_the_code(self):
        exc = classify(_error("05:403001", "Login session has expired."))
        assert "[05:403001]" in str(exc)


class TestMetricLabel:
    def test_prefers_the_platform_code(self):
        exc = classify(_error("05:403001", "expired"))
        assert metric_label(exc) == "05:403001"

    def test_falls_back_to_class_name(self):
        assert metric_label(AuthExpired("no code")) == "AuthExpired"
