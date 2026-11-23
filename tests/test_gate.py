"""The request gate in front of the MCP app, exercised directly as ASGI."""

import pytest

from agentpost import db
from agentpost.server import BearerAuth

pytestmark = pytest.mark.anyio


class Recorder:
    """Stands in for the MCP app and notes what reached it."""

    def __init__(self) -> None:
        self.reached: list[str] = []

    async def __call__(self, scope, receive, send) -> None:
        self.reached.append(scope["type"])


async def no_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


def http_scope(method: str, token: str | None, path: str = "/mcp") -> dict:
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    return {"type": "http", "method": method, "path": path, "headers": headers}


@pytest.fixture
def token(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTPOST_DB", str(tmp_path / "gate.sqlite"))
    db.init()
    return db.add_dev("ana")


async def run(scope: dict) -> tuple[Recorder, list[dict]]:
    inner, sent = Recorder(), []

    async def send(message: dict) -> None:
        sent.append(message)

    await BearerAuth(inner)(scope, no_receive, send)
    return inner, sent


async def test_lifespan_events_pass_through(token):
    inner, _ = await run({"type": "lifespan"})
    assert inner.reached == ["lifespan"]


async def test_websocket_connections_are_refused(token):
    inner, sent = await run({"type": "websocket", "path": "/mcp", "headers": []})
    assert inner.reached == []
    assert sent == [{"type": "websocket.close", "code": 1008}]


async def test_unknown_scope_types_never_reach_the_app(token):
    inner, sent = await run({"type": "something-new"})
    assert inner.reached == [] and sent == []


@pytest.mark.parametrize("method", ["POST", "GET", "DELETE", "PUT"])
async def test_no_method_gets_past_a_missing_or_wrong_token(token, method):
    for bad in (None, "wrong", token + "x"):
        inner, sent = await run(http_scope(method, bad))
        assert inner.reached == []
        assert sent[0]["status"] == 401


@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT", "PATCH", "HEAD"])
async def test_only_post_reaches_the_app(token, method):
    inner, sent = await run(http_scope(method, token))
    assert inner.reached == []
    assert sent[0]["status"] == 405
    assert (b"allow", b"POST") in sent[0]["headers"]


async def test_an_authenticated_post_reaches_the_app_with_the_dev_attached(token):
    scope = http_scope("POST", token)
    inner, _ = await run(scope)
    assert inner.reached == ["http"]
    assert scope["state"]["dev"] == "ana"


async def test_the_dev_cannot_be_preset_by_the_caller(token):
    scope = http_scope("POST", None) | {"state": {"dev": "ana"}}
    inner, sent = await run(scope)
    assert inner.reached == [] and sent[0]["status"] == 401


async def test_health_check_needs_no_token(token):
    inner, sent = await run(http_scope("GET", None, path="/healthz"))
    assert inner.reached == [] and sent[0]["status"] == 200
