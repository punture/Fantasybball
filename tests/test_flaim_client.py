"""Flaim's MCP client and sign-in: JSON-RPC over streamable HTTP, OAuth discovery/registration/PKCE/refresh, the
loopback callback and token storage. Every HTTP exchange is an httpx.MockTransport; only the callback-server test
talks to 127.0.0.1."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from fbm import credentials
from fbm.flaim import client as flaim_client
from fbm.flaim import login, oauth
from fbm.flaim.client import FlaimClient
from fbm.flaim.errors import FlaimError, FlaimLoginRequired

URL = "https://api.flaim.app/mcp"


@pytest.fixture
def vault(monkeypatch):
    """An in-memory OS credential store."""
    store: dict[str, str] = {}
    monkeypatch.setattr(credentials, "get", lambda name: store.get(name))
    monkeypatch.setattr(credentials, "put", lambda name, value: store.__setitem__(name, value))
    monkeypatch.setattr(credentials, "delete", lambda name: store.pop(name, None))
    return store


# ------------------------------------------------------------------ MCP client


class FakeTokens:
    def __init__(self):
        self.token, self.refreshes = "tok-1", 0

    def bearer(self) -> str:
        return self.token

    def refresh(self) -> None:
        self.refreshes += 1
        self.token = f"tok-{self.refreshes + 1}"


def rpc_result(body: dict, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": body["id"], "result": result}


def tool_result(payload: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload)}], "structuredContent": payload}


class FakeServer:
    """A minimal streamable-HTTP MCP server. `script` holds responses for tools/call, used in order."""

    def __init__(self, *script):
        self.script = list(script)
        self.seen: list[tuple[httpx.Request, dict]] = []
        self.inits = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.seen.append((request, body))
        if body.get("method") == "initialize":
            self.inits += 1
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "flaim"}}
            return httpx.Response(200, json=rpc_result(body, result), headers={"mcp-session-id": f"s{self.inits}"})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        step = self.script.pop(0) if self.script else tool_result({"success": True, "data": {}})
        if isinstance(step, httpx.Response):
            return step
        if callable(step):
            return step(request, body)
        return httpx.Response(200, json=rpc_result(body, step))

    def calls(self) -> list[tuple[httpx.Request, dict]]:
        return [(r, b) for r, b in self.seen if b.get("method") == "tools/call"]


def make_client(server: FakeServer, tokens: FakeTokens | None = None, **kwargs) -> tuple[FlaimClient, list[float]]:
    sleeps: list[float] = []
    http = httpx.Client(transport=httpx.MockTransport(server))
    c = FlaimClient(URL, tokens or FakeTokens(), http, min_interval=0, sleep=sleeps.append, **kwargs)
    return c, sleeps


def test_call_tool_initializes_once_and_sends_session_headers():
    server = FakeServer(tool_result({"success": True, "data": {"n": 1}}), tool_result({"success": True, "data": {}}))
    c, _ = make_client(server)
    assert c.call_tool("get_standings", {"league_id": "466.l.1"}) == {"success": True, "data": {"n": 1}}
    c.call_tool("get_matchups", {})
    assert server.inits == 1
    request, body = server.calls()[1]
    assert body["params"] == {"name": "get_matchups", "arguments": {}}
    assert request.headers["authorization"] == "Bearer tok-1"
    assert request.headers["mcp-session-id"] == "s1" and request.headers["mcp-protocol-version"] == "2025-06-18"
    assert "text/event-stream" in request.headers["accept"]


def test_sse_and_text_only_results_are_parsed():
    payload = {"success": True, "data": {"teams": []}}

    def sse(request, body):
        event = json.dumps(rpc_result(body, {"content": [{"type": "text", "text": json.dumps(payload)}]}))
        text = f"event: message\ndata: {event}\n\n"
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})

    c, _ = make_client(FakeServer(sse))
    assert c.call_tool("get_league_info", {}) == payload


def test_tool_errors_raise_flaim_errors():
    auth = {"content": [{"type": "text", "text": "AUTH_FAILED: Authentication required"}], "isError": True}
    missing = {"content": [{"type": "text", "text": "LEAGUE_NOT_FOUND: no such league"}], "isError": True}
    failed = tool_result({"success": False, "error": "Yahoo is down", "code": "YAHOO_API_ERROR"})
    c, _ = make_client(FakeServer(auth, missing, failed))
    with pytest.raises(FlaimLoginRequired, match="fbm flaim login"):
        c.call_tool("get_roster", {})
    with pytest.raises(FlaimError) as err:
        c.call_tool("get_roster", {})
    assert err.value.code == "LEAGUE_NOT_FOUND"
    with pytest.raises(FlaimError, match="Yahoo is down") as err:
        c.call_tool("get_roster", {})
    assert err.value.code == "YAHOO_API_ERROR"


def test_rejected_token_is_refreshed_once():
    tokens = FakeTokens()
    server = FakeServer(httpx.Response(401), tool_result({"success": True, "data": {}}))
    c, _ = make_client(server, tokens)
    c.call_tool("get_standings", {})
    assert tokens.refreshes == 1
    assert server.calls()[-1][0].headers["authorization"] == "Bearer tok-2"
    stubborn = FakeServer(httpx.Response(401), httpx.Response(401))
    c, _ = make_client(stubborn)
    with pytest.raises(FlaimLoginRequired):
        c.call_tool("get_standings", {})


def test_rate_limits_and_server_errors_are_retried_with_backoff():
    server = FakeServer(
        httpx.Response(429, headers={"retry-after": "7"}),
        httpx.Response(503),
        tool_result({"success": True, "data": {}}),
    )
    c, sleeps = make_client(server, max_attempts=3)
    assert c.call_tool("get_standings", {})["success"]
    assert sleeps[:2] == [7.0, 4.0]


def test_a_token_refresh_after_retries_still_resends_the_call():
    tokens = FakeTokens()
    server = FakeServer(httpx.Response(503), httpx.Response(503), httpx.Response(401), tool_result({"success": True}))
    c, _ = make_client(server, tokens, max_attempts=3)
    assert c.call_tool("get_standings", {})["success"] and tokens.refreshes == 1


def test_unreachable_server_is_a_clear_error():
    def down(request):
        raise httpx.ConnectError("no route", request=request)

    http = httpx.Client(transport=httpx.MockTransport(down))
    c = FlaimClient(URL, FakeTokens(), http, min_interval=0, sleep=lambda s: None)
    with pytest.raises(FlaimError, match="can't reach"):
        c.call_tool("get_standings", {})


def test_dropped_session_starts_a_new_one():
    server = FakeServer(httpx.Response(404), tool_result({"success": True, "data": {"ok": 1}}))
    c, _ = make_client(server)
    assert c.call_tool("get_standings", {})["data"] == {"ok": 1}
    assert server.inits == 2 and server.calls()[-1][0].headers["mcp-session-id"] == "s2"


def test_calls_are_paced_and_recorded():
    seen = []
    server = FakeServer(tool_result({"success": True, "data": {"a": 1}}))
    http = httpx.Client(transport=httpx.MockTransport(server))
    sleeps: list[float] = []
    c = FlaimClient(URL, FakeTokens(), http, min_interval=1.1, sleep=sleeps.append, recorder=lambda *a: seen.append(a))
    c.call_tool("get_standings", {"league_id": "x"})
    assert max(sleeps) > 1.0  # three requests (initialize, initialized, call) at least 1.1 s apart
    assert seen == [("get_standings", {"league_id": "x"}, {"success": True, "data": {"a": 1}})]


def test_sse_parser_joins_multiline_data():
    text = 'data: {"jsonrpc": "2.0",\ndata:  "id": 1, "result": {}}\n\n: comment\n\ndata: not json\n\n'
    assert flaim_client._sse_messages(text) == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


def test_client_without_sign_in_says_how_to_sign_in(vault):
    with pytest.raises(FlaimLoginRequired, match="run `fbm flaim login`"):
        FlaimClient(URL, http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))


# ------------------------------------------------------------------ OAuth: discovery, registration, PKCE


def b64_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class AuthServer:
    """Flaim's OAuth endpoints (as its old open-source auth worker serves them)."""

    def __init__(self, prm: bool = True):
        self.prm, self.challenge, self.forms, self.registrations = prm, "", [], []
        self.refresh_ok = True
        self.issued = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/oauth-protected-resource/mcp" and self.prm:
            return httpx.Response(200, json={"resource": URL, "authorization_servers": ["https://api.flaim.app"]})
        if path == "/.well-known/oauth-authorization-server":
            base = "https://api.flaim.app"
            return httpx.Response(
                200,
                json={
                    "issuer": base,
                    "authorization_endpoint": f"{base}/auth/authorize",
                    "token_endpoint": f"{base}/auth/token",
                    "registration_endpoint": f"{base}/auth/register",
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if path == "/auth/register":
            body = json.loads(request.content)
            self.registrations.append(body)
            return httpx.Response(201, json={"client_id": "client-123", **body})
        if path == "/auth/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.forms.append(form)
            if form["grant_type"] == "authorization_code":
                if b64_challenge(form["code_verifier"]) != self.challenge or form["code"] != "the-code":
                    return httpx.Response(400, json={"error": "invalid_grant"})
            elif not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "expired"})
            self.issued += 1
            return httpx.Response(
                200,
                json={"access_token": f"at-{self.issued}", "refresh_token": f"rt-{self.issued}", "expires_in": 3600},
            )
        return httpx.Response(404)


def http_for(server) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(server))


def test_pkce_pair_is_s256():
    verifier, challenge = oauth.pkce_pair()
    assert 43 <= len(verifier) <= 128 and challenge == b64_challenge(verifier) and "=" not in challenge


def test_discovery_follows_protected_resource_metadata():
    ep = oauth.discover(http_for(AuthServer()), URL)
    assert ep.resource == URL and ep.token_endpoint == "https://api.flaim.app/auth/token"
    assert ep.registration_endpoint == "https://api.flaim.app/auth/register"
    assert oauth.discover(http_for(AuthServer(prm=False)), URL).issuer == "https://api.flaim.app"
    with pytest.raises(FlaimError, match="sign-in settings"):
        oauth.discover(http_for(lambda r: httpx.Response(404)), URL)


def test_registration_is_a_public_client_for_the_loopback_redirect():
    server = AuthServer()
    ep = oauth.discover(http_for(server), URL)
    assert oauth.register_client(http_for(server), ep, "http://127.0.0.1:5555/callback") == "client-123"
    body = server.registrations[0]
    assert body["token_endpoint_auth_method"] == "none" and body["redirect_uris"] == ["http://127.0.0.1:5555/callback"]
    assert body["grant_types"] == ["authorization_code", "refresh_token"]
    refused = http_for(lambda r: httpx.Response(400, json={"error": "invalid_redirect_uri"}))
    with pytest.raises(FlaimError, match="invalid_redirect_uri"):
        oauth.register_client(refused, ep, "http://127.0.0.1:1/x")


def test_authorization_url_carries_pkce_state_and_resource():
    ep = oauth.Endpoints(URL, "https://api.flaim.app", "https://api.flaim.app/auth/authorize", "t", "r")
    url = oauth.authorization_url(ep, "cid", "http://127.0.0.1:9/callback", "chal", "st8")
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q == {
        "response_type": "code",
        "client_id": "cid",
        "redirect_uri": "http://127.0.0.1:9/callback",
        "code_challenge": "chal",
        "code_challenge_method": "S256",
        "state": "st8",
        "scope": "mcp:read",
        "resource": URL,
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("http://127.0.0.1:9/callback?code=abc&state=s1", "abc"),
        ("code=abc&state=s1", "abc"),
        ("  abc123  ", "abc123"),
    ],
)
def test_code_from_redirect(text, expected):
    assert oauth.code_from_redirect(text, "s1") == expected


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("http://127.0.0.1:9/callback?code=abc&state=other", "different login attempt"),
        ("http://127.0.0.1:9/callback?error=access_denied&error_description=no", "access_denied"),
        ("http://127.0.0.1:9/callback?foo=1", "no authorization code"),
        ("", "no sign-in response"),
    ],
)
def test_code_from_redirect_rejects(text, message):
    with pytest.raises(FlaimError, match=message):
        oauth.code_from_redirect(text, "s1")


# ------------------------------------------------------------------ tokens: exchange, refresh, storage


def signed_in(now: float = 1000.0, **changes) -> oauth.OAuthState:
    ep = oauth.Endpoints(URL, "https://api.flaim.app", "a", "https://api.flaim.app/auth/token", "r")
    state = oauth.new_state(URL, ep, "client-123", "http://127.0.0.1:9/callback")
    state.access_token, state.refresh_token = "at-0", "rt-0"
    state.expires_at, state.refresh_issued_at, state.signed_in_at = now + 3600, now, now
    for k, v in changes.items():
        setattr(state, k, v)
    return state


def test_refresh_rotates_and_saves_the_new_refresh_token(vault):
    server = AuthServer()
    clock = [1000.0 + 3600]  # the access token has just expired
    session = oauth.TokenSession(http_for(server), signed_in(), clock=lambda: clock[0])
    assert session.bearer() == "at-1"
    form = server.forms[-1]
    assert (
        form["grant_type"] == "refresh_token" and form["refresh_token"] == "rt-0" and form["client_id"] == "client-123"
    )
    stored = oauth.load()
    assert stored.refresh_token == "rt-1" and stored.refresh_issued_at == clock[0]
    assert stored.expires_at == clock[0] + 3600
    assert session.bearer() == "at-1" and len(server.forms) == 1  # still valid: no second refresh


def test_expired_refresh_token_asks_for_login(vault):
    server = AuthServer()
    server.refresh_ok = False
    session = oauth.TokenSession(http_for(server), signed_in(), clock=lambda: 10**9)
    with pytest.raises(FlaimLoginRequired, match="fbm flaim login"):
        session.bearer()


def test_refresh_race_adopts_the_other_processs_tokens(vault):
    server = AuthServer()
    server.refresh_ok = False  # rt-0 was already rotated by another FBM process...
    oauth.save(signed_in(access_token="at-other", refresh_token="rt-other", expires_at=10**10))  # ...which saved this
    session = oauth.TokenSession(http_for(server), signed_in(), clock=lambda: 5000.0)
    assert session.bearer() == "at-other"


def test_state_round_trips_and_summary_has_no_secrets(vault):
    state = signed_in()
    oauth.save(state)
    raw = vault[credentials.FLAIM_OAUTH]
    assert raw.startswith("chunks:")  # stored via put_large (Windows Credential Manager size limit)
    assert oauth.load() == state
    summary = json.dumps(state.summary(now=1000.0 + 86400))
    assert "at-0" not in summary and "rt-0" not in summary and "client-123" not in summary
    assert state.summary(now=1000.0 + 86400)["refresh_days_left"] == 6.0
    assert state.summary(now=1000.0 + 8 * 86400)["signed_in"] is False
    newer = json.loads(state.to_json()) | {"added_later": 1}
    assert oauth.OAuthState.from_json(json.dumps(newer)) == state
    oauth.clear()
    assert oauth.load() is None and not vault


def test_unreadable_stored_state_reads_as_signed_out(vault):
    credentials.put_large(credentials.FLAIM_OAUTH, "not json")
    assert oauth.load() is None


# ------------------------------------------------------------------ the login flow


def test_sign_in_with_a_pasted_redirect(vault):
    server = AuthServer()
    printed: list[str] = []

    def prompt(question: str) -> str:
        url = next(line for line in printed if "/auth/authorize?" in line).split("\n")[-1]
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        server.challenge = q["code_challenge"]
        return f"{q['redirect_uri']}?code=the-code&state={q['state']}"

    state = login.sign_in(
        http_for(server), URL, open_browser=False, echo=printed.append, prompt=prompt, clock=lambda: 2000.0
    )
    assert (state.client_id, state.access_token, state.refresh_token) == ("client-123", "at-1", "rt-1")
    assert state.signed_in_at == 2000.0 and state.expires_at == 5600.0 and state.refresh_issued_at == 2000.0
    assert state.redirect_uri.startswith("http://127.0.0.1:") and state.redirect_uri.endswith("/callback")
    exchange = server.forms[-1]
    assert exchange["redirect_uri"] == state.redirect_uri and exchange["resource"] == URL
    assert not vault  # the caller decides to save


def test_sign_in_through_the_browser_and_loopback_callback(vault):
    server = AuthServer()

    def browser(url: str) -> bool:
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        server.challenge = q["code_challenge"]
        target = f"{q['redirect_uri']}?code=the-code&state={q['state']}"
        threading.Thread(target=lambda: httpx.get(target, trust_env=False), daemon=True).start()
        return True

    state = login.sign_in(http_for(server), URL, opener=browser, echo=lambda s: None, timeout_s=10, clock=lambda: 1.0)
    assert state.access_token == "at-1"


def test_callback_server_only_answers_the_callback_path():
    server = login.CallbackServer().start()
    try:
        base = server.redirect_uri.rsplit("/", 1)[0]
        assert httpx.get(f"{base}/favicon.ico", trust_env=False).status_code == 404
        assert server.wait(0.01) is None
        resp = httpx.get(f"{server.redirect_uri}?code=c&state=s", trust_env=False)
        assert resp.status_code == 200 and "signed in" in resp.text
        assert server.wait(1) == f"{server.redirect_uri}?code=c&state=s"
    finally:
        server.close()


def test_sign_in_times_out_with_a_tip():
    server = AuthServer()
    with pytest.raises(FlaimError, match="--no-browser"):
        login.sign_in(http_for(server), URL, opener=lambda url: True, echo=lambda s: None, timeout_s=0.05)
