"""HikerAPI HTTP client tests — auth header, retries, pagination."""

from cryptography.fernet import Fernet
import httpx
import pytest


@pytest.fixture(autouse=True)
def _setup_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("IG_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("USE_HIKERAPI", "true")
    monkeypatch.setenv("HIKERAPI_KEY", "test-key-123")
    monkeypatch.setenv("HIKERAPI_BASE_URL", "https://api.hikerapi.test")
    monkeypatch.setenv("HIKERAPI_MAX_RETRIES", "2")
    monkeypatch.setenv("HIKERAPI_PAGE_SIZE", "5")
    import importlib

    import app.core.config as cfg

    importlib.reload(cfg)
    import app.services.scrapers.hikerapi.client as hk_client

    importlib.reload(hk_client)
    return hk_client


@pytest.mark.asyncio
async def test_auth_header_attached(_setup_env, monkeypatch):
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(_handler)

    async with _setup_env.HikerAPIClient() as client:
        client._http = httpx.AsyncClient(
            base_url="https://api.hikerapi.test",
            transport=transport,
            headers={"x-access-key": "test-key-123", "accept": "application/json"},
        )
        result = await client.get("/v2/user/by/username", username="ronaldo")

    assert result == {"ok": True}
    assert captured["headers"]["x-access-key"] == "test-key-123"
    assert "username=ronaldo" in captured["url"]


@pytest.mark.asyncio
async def test_404_raises_not_found(_setup_env):
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    async with _setup_env.HikerAPIClient() as client:
        client._http = httpx.AsyncClient(
            base_url="https://api.hikerapi.test",
            transport=httpx.MockTransport(_handler),
        )
        with pytest.raises(_setup_env.HikerAPINotFound):
            await client.get("/v2/user/by/username", username="x")


@pytest.mark.asyncio
async def test_402_quota_exceeded(_setup_env):
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": "billing"})

    async with _setup_env.HikerAPIClient() as client:
        client._http = httpx.AsyncClient(
            base_url="https://api.hikerapi.test",
            transport=httpx.MockTransport(_handler),
        )
        with pytest.raises(_setup_env.HikerAPIQuotaExceeded):
            await client.get("/v2/user/by/username", username="x")


@pytest.mark.asyncio
async def test_5xx_retried_then_succeeds(_setup_env, monkeypatch):
    """Server-error then-success flow exercises the retry loop."""

    async def _no_sleep(*_a, **_kw):
        return None

    monkeypatch.setattr(_setup_env.asyncio, "sleep", _no_sleep)

    calls = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True, "attempt": calls["n"]})

    async with _setup_env.HikerAPIClient() as client:
        client._http = httpx.AsyncClient(
            base_url="https://api.hikerapi.test",
            transport=httpx.MockTransport(_handler),
        )
        result = await client.get("/v2/anything")

    assert result == {"ok": True, "attempt": 2}
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_pagination_walks_cursor(_setup_env):
    """paginate_chunks should follow `end_cursor` until exhausted."""

    pages = [
        {"medias": [{"pk": 1}, {"pk": 2}], "end_cursor": "page2"},
        {"medias": [{"pk": 3}, {"pk": 4}], "end_cursor": "page3"},
        {"medias": [{"pk": 5}], "end_cursor": None},
    ]
    cursor_to_idx = {None: 0, "page2": 1, "page3": 2}

    def _handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("end_cursor") or None
        idx = cursor_to_idx[cursor]
        return httpx.Response(200, json=pages[idx])

    async with _setup_env.HikerAPIClient() as client:
        client._http = httpx.AsyncClient(
            base_url="https://api.hikerapi.test",
            transport=httpx.MockTransport(_handler),
        )
        out = []
        async for media in client.paginate_chunks(
            "/v2/user/medias/chunk", items_key="medias", user_id=42
        ):
            out.append(media["pk"])

    assert out == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_pagination_stops_at_max_items(_setup_env):
    pages = [
        {"medias": [{"pk": 1}, {"pk": 2}, {"pk": 3}], "end_cursor": "p2"},
        {"medias": [{"pk": 4}, {"pk": 5}], "end_cursor": None},
    ]
    cursor_to_idx = {None: 0, "p2": 1}

    def _handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("end_cursor") or None
        return httpx.Response(200, json=pages[cursor_to_idx[cursor]])

    async with _setup_env.HikerAPIClient() as client:
        client._http = httpx.AsyncClient(
            base_url="https://api.hikerapi.test",
            transport=httpx.MockTransport(_handler),
        )
        out = []
        async for media in client.paginate_chunks(
            "/v2/user/medias/chunk", items_key="medias", max_items=2
        ):
            out.append(media["pk"])

    assert out == [1, 2]


@pytest.mark.asyncio
async def test_privacy_check_default_false(_setup_env):
    """Cost optimisation: privacy_check=false on every call by default."""
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": True})

    async with _setup_env.HikerAPIClient() as client:
        client._http = httpx.AsyncClient(
            base_url="https://api.hikerapi.test",
            transport=httpx.MockTransport(_handler),
        )
        await client.get("/v2/user/by/username", username="ronaldo")

    assert captured["params"].get("privacy_check") == "false"
    assert captured["params"].get("username") == "ronaldo"


@pytest.mark.asyncio
async def test_privacy_check_caller_override(_setup_env):
    """Caller can opt-in per-call when they explicitly need the check."""
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": True})

    async with _setup_env.HikerAPIClient() as client:
        client._http = httpx.AsyncClient(
            base_url="https://api.hikerapi.test",
            transport=httpx.MockTransport(_handler),
        )
        await client.get("/v2/user/by/username", username="ronaldo", privacy_check="true")

    assert captured["params"]["privacy_check"] == "true"


@pytest.mark.asyncio
async def test_pagination_stops_when_predicate(_setup_env):
    pages = [
        {"medias": [{"pk": 1}, {"pk": 2}, {"pk": 99}, {"pk": 4}], "end_cursor": None},
    ]

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[0])

    async with _setup_env.HikerAPIClient() as client:
        client._http = httpx.AsyncClient(
            base_url="https://api.hikerapi.test",
            transport=httpx.MockTransport(_handler),
        )
        out = []
        async for media in client.paginate_chunks(
            "/v2/user/medias/chunk",
            items_key="medias",
            stop_when=lambda m: m["pk"] == 99,
        ):
            out.append(media["pk"])

    assert out == [1, 2]
