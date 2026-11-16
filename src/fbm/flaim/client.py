"""A small synchronous MCP client for Flaim (JSON-RPC 2.0 over streamable HTTP).

Why not the official `mcp` SDK: it is async-only and heavy (Starlette, uvicorn, OpenTelemetry, ...), and it does
not persist when an access token expires, so after a restart it sends a stale token and falls into its interactive
re-authorization flow instead of using the stored refresh token. A headless daily job needs exactly that refresh,
so FBM speaks the few messages it needs directly with httpx (already a dependency): `initialize`,
`notifications/initialized`, `tools/list` and `tools/call`.

Calls are paced (Flaim allows 60 requests a minute per user), transient failures are retried, and a token Flaim
rejects is refreshed once; an expired sign-in raises FlaimLoginRequired ("run `fbm flaim login`").
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from fbm.flaim import oauth
from fbm.flaim.errors import FlaimError, FlaimLoginRequired

PROTOCOL_VERSION = "2025-06-18"
MIN_INTERVAL_S = 1.1  # Flaim's limit is 60 MCP requests a minute per user
RETRYABLE = {408, 425, 429, 500, 502, 503, 504}
MAX_BACKOFF_S = 30.0
CLIENT_INFO = {"name": "fbm", "version": "0.1"}

Recorder = Callable[[str, dict[str, Any], dict[str, Any]], None]  # (tool, arguments, payload): raw dumps
_last_request = [0.0]  # monotonic time of this process's latest Flaim request


def default_url() -> str:
    from fbm.settings import load_settings

    return load_settings().flaim_url


def _sse_messages(text: str) -> list[dict[str, Any]]:
    """JSON-RPC messages from a text/event-stream body (each event's `data:` lines joined)."""
    out, data = [], []
    for line in [*text.splitlines(), ""]:
        if line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        elif not line.strip() and data:
            try:
                msg = json.loads("\n".join(data))
            except ValueError:
                msg = None
            if isinstance(msg, dict):
                out.append(msg)
            data = []
    return out


def _error_code(text: str) -> str:
    """Flaim tool errors read "CODE: message"."""
    head, sep, _ = text.partition(":")
    return head.strip() if sep and head.strip().replace("_", "").isalnum() and head.strip().isupper() else ""


def tool_payload(tool: str, result: dict[str, Any]) -> dict[str, Any]:
    """A tool result -> its JSON payload ({success, data, ...}); errors raise FlaimError."""
    texts = [c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"]
    if result.get("isError"):
        message = (texts[0] if texts else "tool error").strip()
        code = _error_code(message)
        if code in ("AUTH_FAILED", "UNAUTHORIZED"):
            raise FlaimLoginRequired("Flaim rejected the sign-in")
        raise FlaimError(message[:300], code=code, tool=tool)
    payload = result.get("structuredContent")
    if not isinstance(payload, dict):
        try:
            payload = json.loads(texts[0]) if texts else None
        except ValueError:
            payload = None
    if not isinstance(payload, dict):
        raise FlaimError(f"{tool} returned no JSON data", tool=tool)
    if payload.get("success") is False:
        message = str(payload.get("error") or payload.get("message") or "the tool reported a failure")
        raise FlaimError(f"{tool}: {message[:300]}", code=str(payload.get("code") or ""), tool=tool)
    return payload


class FlaimClient:
    """One MCP session with Flaim, signed in with the stored OAuth tokens (fbm.flaim.oauth)."""

    auth_mode = "flaim"

    def __init__(
        self,
        url: str | None = None,
        tokens: oauth.TokenSession | None = None,
        http: httpx.Client | None = None,
        *,
        min_interval: float = MIN_INTERVAL_S,
        max_attempts: int = 3,
        timeout: float = 45.0,
        sleep: Callable[[float], None] = time.sleep,
        recorder: Recorder | None = None,
    ):
        self.url = url or default_url()
        self._http = http or httpx.Client(timeout=timeout, headers={"User-Agent": "fbm/0.1"})
        self._own_http = http is None
        try:
            self._tokens = tokens or oauth.TokenSession(self._http)
        except Exception:
            self.close()
            raise
        self._min_interval = min_interval
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._session_id: str | None = None
        self._protocol: str | None = None
        self._next_id = 0
        self.recorder = recorder

    def close(self) -> None:
        if self._own_http:
            self._http.close()

    def __enter__(self) -> FlaimClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ public API

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run one Flaim tool: its JSON payload ({success, data, ...}). FlaimError on a tool or transport failure."""
        args = dict(arguments or {})
        payload = tool_payload(name, self._rpc("tools/call", {"name": name, "arguments": args}))
        if self.recorder is not None:
            self.recorder(name, args, payload)
        return payload

    def list_tools(self) -> list[dict[str, Any]]:
        return list(self._rpc("tools/list", {}).get("tools") or [])

    # ------------------------------------------------------------------ JSON-RPC over streamable HTTP

    def _pace(self) -> None:
        """Space requests from every client in this process (one snapshot opens several)."""
        wait = _last_request[0] + self._min_interval - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        _last_request[0] = time.monotonic()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._tokens.bearer()}",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._protocol:
            headers["MCP-Protocol-Version"] = self._protocol
        return headers

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        """POST one message: transient failures retried (up to `max_attempts`), a rejected token refreshed once and
        the message re-sent (that retry is not counted as a failed attempt)."""
        refreshed = False
        attempt = 0
        while True:
            self._pace()
            try:
                resp = self._http.post(self.url, json=body, headers=self._headers())
            except httpx.TransportError as err:
                if attempt >= self._max_attempts - 1:
                    raise FlaimError(f"can't reach {self.url} ({type(err).__name__})") from err
                self._sleep(min(2**attempt, MAX_BACKOFF_S))
                attempt += 1
                continue
            if resp.status_code == 401 and not refreshed:
                self._tokens.refresh()
                refreshed = True
                continue
            if resp.status_code in RETRYABLE and attempt < self._max_attempts - 1:
                self._sleep(_retry_after(resp, attempt))
                attempt += 1
                continue
            break
        if resp.status_code == 401:
            raise FlaimLoginRequired("Flaim rejected the sign-in")
        return resp

    def _initialize(self) -> None:
        self._session_id, self._protocol = None, None
        params = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO}
        resp = self._post(self._message("initialize", params))
        result = self._result(resp, "initialize")
        self._session_id = resp.headers.get("mcp-session-id") or None
        self._protocol = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _message(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        return {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._protocol is None:
            self._initialize()
        resp = self._post(self._message(method, params))
        if resp.status_code == 404 and self._session_id:  # the server dropped our session: start a new one, once
            self._initialize()
            resp = self._post(self._message(method, params))
        return self._result(resp, method)

    def _result(self, resp: httpx.Response, method: str) -> dict[str, Any]:
        if resp.status_code >= 400:
            raise FlaimError(f"{method} failed: HTTP {resp.status_code}", status=resp.status_code, tool=method)
        ctype = resp.headers.get("content-type", "").lower()
        if ctype.startswith("text/event-stream"):
            messages = [m for m in _sse_messages(resp.text) if "result" in m or "error" in m]
        else:
            try:
                body = resp.json()
            except ValueError as err:
                raise FlaimError(f"{method} returned an unreadable response", tool=method) from err
            messages = body if isinstance(body, list) else [body]
        msg = next((m for m in messages if isinstance(m, dict) and ("result" in m or "error" in m)), None)
        if msg is None:
            raise FlaimError(f"{method} returned no result", tool=method)
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}
            raise FlaimError(f"{method} error {err.get('code', '')}: {err.get('message', '')}".strip(), tool=method)
        return msg["result"] if isinstance(msg["result"], dict) else {}


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    try:
        return min(max(float(resp.headers.get("retry-after", "")), 1.0), MAX_BACKOFF_S)
    except ValueError:
        return min(2.0 ** (attempt + 1), MAX_BACKOFF_S)
