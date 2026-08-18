"""Tests for the client's guards and retry/refresh behaviour.

The HTTP layer is stubbed by monkeypatching the client's `httpx.AsyncClient`
`post`, so these run without a network or a database.
"""

import json

import pytest

from app.core.config import settings
from app.services.youcloud.client import YouCloudClient
from app.services.youcloud.errors import AuthExpired, BadFilter, PlanDenied, TransportError


class _FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload) if payload is not None else text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _client(session="cookie-value"):
    async def provider():
        return session

    return YouCloudClient(session_provider=provider)


def _ok_payload(rows=1, total=100):
    return {
        "data": {
            "materialList": {
                "page": 1,
                "total": total,
                "limit": 50,
                "data": [{"material": {"id": f"m{i}"}} for i in range(rows)],
            }
        }
    }


def _error_payload(code=None, message=None):
    extensions = {}
    if code:
        extensions["c"] = code
    if message:
        extensions["m"] = message
    return {"errors": [{"message": None, "extensions": extensions}], "data": None}


class TestPageCeiling:
    """The API refuses page > 200; we refuse it before spending a request."""

    def test_accepts_the_last_valid_page(self):
        YouCloudClient.assert_page_within_ceiling(settings.AD_MAX_PAGE)

    def test_rejects_one_past_the_ceiling(self):
        with pytest.raises(BadFilter) as info:
            YouCloudClient.assert_page_within_ceiling(settings.AD_MAX_PAGE + 1)
        # The message has to tell the operator what to do instead.
        assert "Narrow the filter" in str(info.value)

    @pytest.mark.parametrize("page", [0, -1])
    def test_rejects_non_positive(self, page):
        with pytest.raises(BadFilter):
            YouCloudClient.assert_page_within_ceiling(page)

    @pytest.mark.parametrize("page", ["5", 1.5, None, True])
    def test_rejects_non_int(self, page):
        with pytest.raises(BadFilter):
            YouCloudClient.assert_page_within_ceiling(page)


class TestExecute:
    async def test_returns_data_on_success(self, monkeypatch):
        client = _client()

        async def fake_post(*args, **kwargs):
            return _FakeResponse(_ok_payload())

        monkeypatch.setattr(client._http, "post", fake_post)
        data = await client.execute("query {}", {}, operation_name="materialList")
        assert data["materialList"]["total"] == 100
        await client.aclose()

    async def test_raises_without_a_session_cookie(self):
        client = _client(session=None)
        with pytest.raises(AuthExpired):
            await client.execute("query {}", {}, operation_name="materialList")
        await client.aclose()

    async def test_plan_denied_is_terminal_and_not_retried(self, monkeypatch):
        client = _client()
        calls = []

        async def fake_post(*args, **kwargs):
            calls.append(1)
            return _FakeResponse(_error_payload("00:403001", "Permission denied, please upgrade your plan"))

        monkeypatch.setattr(client._http, "post", fake_post)
        with pytest.raises(PlanDenied):
            await client.execute("query {}", {}, operation_name="materialList")
        assert len(calls) == 1, "PlanDenied must not be retried"
        await client.aclose()

    async def test_bad_filter_is_terminal_and_not_retried(self, monkeypatch):
        client = _client()
        calls = []

        async def fake_post(*args, **kwargs):
            calls.append(1)
            return _FakeResponse(_error_payload(None, "Parameter error, please clear the filter and refresh"))

        monkeypatch.setattr(client._http, "post", fake_post)
        with pytest.raises(BadFilter):
            await client.execute("query {}", {}, operation_name="materialList")
        assert len(calls) == 1
        await client.aclose()

    async def test_auth_expired_is_terminal_and_not_retried(self, monkeypatch):
        """Only an operator can mint a new token, so retrying reaches the
        same conclusion later while burning the job's budget."""
        client = _client()
        calls = []

        async def fake_post(*args, **kwargs):
            calls.append(1)
            return _FakeResponse(_error_payload("05:403001", "Login session has expired. Please log in again."))

        monkeypatch.setattr(client._http, "post", fake_post)
        with pytest.raises(AuthExpired) as info:
            await client.execute("query {}", {}, operation_name="materialList")
        assert len(calls) == 1, "AuthExpired must not be retried"
        # The platform's own code has to survive onto the job row.
        assert info.value.code == "05:403001"
        await client.aclose()

    async def test_missing_token_names_the_fix(self, monkeypatch):
        """The message lands on the job row, so it must say what to do."""
        client = _client(session=None)
        with pytest.raises(AuthExpired) as info:
            await client.execute("query {}", {}, operation_name="materialList")
        assert "credentials/session" in str(info.value)
        await client.aclose()

    async def test_non_2xx_is_a_transport_error(self, monkeypatch):
        """HTTP 406 happens when accept-language is stripped; body is plain text."""
        client = _client()
        monkeypatch.setattr(settings, "AD_API_MAX_RETRIES", 1)

        async def fake_post(*args, **kwargs):
            return _FakeResponse(None, status_code=406, text="The Language: [] is no acceptable")

        monkeypatch.setattr(client._http, "post", fake_post)
        with pytest.raises(TransportError) as info:
            await client.execute("query {}", {}, operation_name="materialList")
        assert "406" in str(info.value)
        await client.aclose()

    async def test_non_json_body_is_a_transport_error(self, monkeypatch):
        client = _client()
        monkeypatch.setattr(settings, "AD_API_MAX_RETRIES", 1)

        async def fake_post(*args, **kwargs):
            return _FakeResponse(None, status_code=200, text="<html>gateway</html>")

        monkeypatch.setattr(client._http, "post", fake_post)
        with pytest.raises(TransportError):
            await client.execute("query {}", {}, operation_name="materialList")
        await client.aclose()

    async def test_sends_the_mandatory_headers(self, monkeypatch):
        """accept-language is required — its absence yields HTTP 406."""
        client = _client()
        captured = {}

        async def fake_post(url, **kwargs):
            captured["url"] = url
            captured["cookies"] = kwargs.get("cookies")
            captured["headers"] = kwargs.get("headers")
            return _FakeResponse(_ok_payload())

        monkeypatch.setattr(client._http, "post", fake_post)
        await client.execute("query {}", {}, operation_name="materialList")

        assert client._http.headers["accept-language"] == settings.AD_API_LANGUAGE
        assert client._http.headers["origin"] == settings.AD_API_ORIGIN
        assert captured["cookies"]["sessionId"] == "cookie-value"
        assert captured["headers"]["x-operation-name"] == "materialList"
        await client.aclose()


class TestPaginate:
    async def test_walks_the_requested_window(self, monkeypatch):
        client = _client()
        monkeypatch.setattr(settings, "AD_API_PAGE_DELAY_SECONDS", 0)
        seen_pages = []

        async def fake_post(url, **kwargs):
            seen_pages.append(kwargs["json"]["variables"]["page"])
            # `total` has to justify the window: 50 rows/page x page 4 = 200,
            # so a total below 200 would (correctly) stop the walk early.
            return _FakeResponse(_ok_payload(rows=50, total=5_000))

        monkeypatch.setattr(client._http, "post", fake_post)
        pages = [page async for page, _ in client.paginate_materials({}, page_from=2, page_to=4)]
        assert pages == [2, 3, 4]
        assert seen_pages == [2, 3, 4]
        await client.aclose()

    async def test_stops_on_the_first_empty_page(self, monkeypatch):
        client = _client()
        monkeypatch.setattr(settings, "AD_API_PAGE_DELAY_SECONDS", 0)
        responses = [_FakeResponse(_ok_payload(rows=2)), _FakeResponse(_ok_payload(rows=0))]

        async def fake_post(*args, **kwargs):
            return responses.pop(0)

        monkeypatch.setattr(client._http, "post", fake_post)
        pages = [page async for page, _ in client.paginate_materials({}, page_from=1, page_to=10)]
        assert pages == [1, 2], "should stop after the empty page, not walk to 10"
        await client.aclose()

    async def test_stops_when_total_is_reached(self, monkeypatch):
        """Past the end the API REPEATS the last page instead of returning
        empty — measured: a filter with total=26 answers pages 1, 2 and 3 with
        the identical 26 rows. Without a `total` bound, page_to=200 would
        spend 200 requests fetching the same 26 creatives 200 times.
        """
        client = _client()
        monkeypatch.setattr(settings, "AD_API_PAGE_DELAY_SECONDS", 0)
        requested = []

        async def fake_post(url, **kwargs):
            requested.append(kwargs["json"]["variables"]["page"])
            return _FakeResponse(_ok_payload(rows=26, total=26))

        monkeypatch.setattr(client._http, "post", fake_post)
        pages = [page async for page, _ in client.paginate_materials({}, page_from=1, page_to=200)]
        assert pages == [1], "one page covers total=26; the rest would be repeats"
        assert requested == [1], "must not spend 199 wasted requests"
        await client.aclose()

    async def test_walks_every_page_a_large_total_justifies(self, monkeypatch):
        client = _client()
        monkeypatch.setattr(settings, "AD_API_PAGE_DELAY_SECONDS", 0)

        async def fake_post(*args, **kwargs):
            return _FakeResponse(_ok_payload(rows=50, total=10_000))

        monkeypatch.setattr(client._http, "post", fake_post)
        pages = [page async for page, _ in client.paginate_materials({}, page_from=1, page_to=4)]
        assert pages == [1, 2, 3, 4]
        await client.aclose()

    async def test_total_bound_uses_the_payload_limit(self, monkeypatch):
        """`limit` is server-fixed, but trust the payload so a server-side
        change cannot silently break the bound.
        """
        client = _client()
        monkeypatch.setattr(settings, "AD_API_PAGE_DELAY_SECONDS", 0)

        async def fake_post(*args, **kwargs):
            payload = _ok_payload(rows=10, total=20)
            payload["data"]["materialList"]["limit"] = 10
            return _FakeResponse(payload)

        monkeypatch.setattr(client._http, "post", fake_post)
        pages = [page async for page, _ in client.paginate_materials({}, page_from=1, page_to=10)]
        assert pages == [1, 2], "20 rows / limit 10 = 2 pages"
        await client.aclose()

    async def test_missing_total_falls_back_to_the_empty_page_stop(self, monkeypatch):
        client = _client()
        monkeypatch.setattr(settings, "AD_API_PAGE_DELAY_SECONDS", 0)
        responses = [
            _FakeResponse({"data": {"materialList": {"page": 1, "limit": 50, "data": [{"material": {"id": "a"}}]}}}),
            _FakeResponse({"data": {"materialList": {"page": 2, "limit": 50, "data": []}}}),
        ]

        async def fake_post(*args, **kwargs):
            return responses.pop(0)

        monkeypatch.setattr(client._http, "post", fake_post)
        pages = [page async for page, _ in client.paginate_materials({}, page_from=1, page_to=10)]
        assert pages == [1, 2]
        await client.aclose()

    async def test_rejects_a_window_past_the_ceiling_before_requesting(self, monkeypatch):
        client = _client()
        called = []

        async def fake_post(*args, **kwargs):
            called.append(1)
            return _FakeResponse(_ok_payload())

        monkeypatch.setattr(client._http, "post", fake_post)
        with pytest.raises(BadFilter):
            async for _ in client.paginate_materials({}, page_from=1, page_to=settings.AD_MAX_PAGE + 1):
                pass
        assert not called, "must not spend a request on a window the server will reject"
        await client.aclose()

    async def test_rejects_inverted_window(self, monkeypatch):
        client = _client()
        with pytest.raises(BadFilter):
            async for _ in client.paginate_materials({}, page_from=5, page_to=2):
                pass
        await client.aclose()

    async def test_injects_page_and_order_without_mutating_the_caller_dict(self, monkeypatch):
        client = _client()
        monkeypatch.setattr(settings, "AD_API_PAGE_DELAY_SECONDS", 0)
        filters = {"purpose": 2, "media": [2]}
        captured = {}

        async def fake_post(url, **kwargs):
            captured.update(kwargs["json"]["variables"])
            return _FakeResponse(_ok_payload(rows=0))

        monkeypatch.setattr(client._http, "post", fake_post)
        async for _ in client.paginate_materials(filters, page_from=1, page_to=1, order="impression_desc"):
            pass

        assert captured["page"] == 1
        assert captured["order"] == "impression_desc"
        assert captured["purpose"] == 2
        assert filters == {"purpose": 2, "media": [2]}, "caller's filters must not be mutated"
        await client.aclose()
