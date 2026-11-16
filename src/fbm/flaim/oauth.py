"""Flaim sign-in (OAuth 2.1 as MCP specifies it): discovery, dynamic client registration, PKCE and token refresh.

FBM registers itself as a public client (no secret) for a loopback redirect, the owner approves once in a browser,
and the client id plus tokens are kept in the OS credential store under `credentials.FLAIM_OAUTH` (never in a file,
never printed). Access tokens last about an hour. Every refresh rotates the refresh token, which lasts 7 days, so a
daily FBM job keeps the sign-in alive; more than 7 days without any refresh means `fbm flaim login` again.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from fbm import credentials
from fbm.flaim.errors import FlaimError, FlaimLoginRequired

SCOPE = "mcp:read"
CLIENT_NAME = "FBM fantasy basketball GM"
REFRESH_TOKEN_DAYS = 7  # Flaim refresh tokens last 7 days; each refresh issues a new one
EXPIRY_MARGIN_S = 60  # refresh this long before the access token runs out
_LOGIN_ERRORS = ("invalid_grant", "invalid_client", "unauthorized_client")


@dataclass(frozen=True)
class Endpoints:
    resource: str  # the protected resource tokens are issued for (Flaim's MCP URL)
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str


@dataclass
class OAuthState:
    """Everything FBM keeps about its Flaim sign-in (stored as one JSON blob in the OS credential store)."""

    server_url: str
    resource: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    client_id: str
    redirect_uri: str
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0  # epoch seconds the access token expires
    refresh_issued_at: float = 0.0  # epoch seconds the current refresh token was issued (7-day window)
    signed_in_at: float = 0.0

    def access_valid(self, now: float) -> bool:
        return bool(self.access_token) and now < self.expires_at - EXPIRY_MARGIN_S

    @property
    def refresh_expires_at(self) -> float:
        return self.refresh_issued_at + REFRESH_TOKEN_DAYS * 86400 if self.refresh_issued_at else 0.0

    def summary(self, now: float | None = None) -> dict[str, Any]:
        """Non-secret status: what `fbm flaim status` and the health check show."""
        now = time.time() if now is None else now
        refresh_left = (self.refresh_expires_at - now) / 86400 if self.refresh_expires_at else None
        return {
            "server": self.server_url,
            "signed_in_utc": _iso(self.signed_in_at),
            "access_token_expires_utc": _iso(self.expires_at),
            "access_token_valid": self.access_valid(now),
            "refresh_window_ends_utc": _iso(self.refresh_expires_at),
            "refresh_days_left": None if refresh_left is None else round(refresh_left, 1),
            "signed_in": bool(self.refresh_token) and (refresh_left is None or refresh_left > 0),
        }

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> OAuthState:
        data = json.loads(raw)
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def _iso(epoch: float) -> str | None:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds") if epoch else None


# ------------------------------------------------------------------ storage (OS credential store)


def load() -> OAuthState | None:
    try:
        raw = credentials.get_large(credentials.FLAIM_OAUTH)
    except Exception as err:  # noqa: BLE001 - e.g. no usable OS credential store (headless Linux without a keyring)
        raise FlaimError(f"can't read the Flaim sign-in from your OS credential store ({type(err).__name__})") from err
    if not raw:
        return None
    try:
        return OAuthState.from_json(raw)
    except (ValueError, TypeError):
        return None


def save(state: OAuthState) -> None:
    credentials.put_large(credentials.FLAIM_OAUTH, state.to_json())


def clear() -> None:
    credentials.delete_large(credentials.FLAIM_OAUTH)


# ------------------------------------------------------------------ discovery and registration


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _get_json(http: httpx.Client, url: str) -> dict[str, Any] | None:
    try:
        resp = http.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def discover(http: httpx.Client, server_url: str) -> Endpoints:
    """RFC 9728 protected-resource metadata names the authorization server; RFC 8414 metadata names its endpoints."""
    origin, path = _origin(server_url), urlparse(server_url).path.rstrip("/")
    prm = (
        _get_json(http, f"{origin}/.well-known/oauth-protected-resource{path}")
        or _get_json(http, f"{origin}/.well-known/oauth-protected-resource")
        or {}
    )
    servers = prm.get("authorization_servers") or [origin]
    issuer = str(servers[0]).rstrip("/")
    as_origin, as_path = _origin(issuer), urlparse(issuer).path.rstrip("/")
    candidates = (
        f"{as_origin}/.well-known/oauth-authorization-server{as_path}",
        f"{issuer}/.well-known/oauth-authorization-server",
        f"{as_origin}/.well-known/openid-configuration{as_path}",
    )
    meta = next((m for url in dict.fromkeys(candidates) if (m := _get_json(http, url))), None)
    if meta is None:
        raise FlaimError(f"couldn't read Flaim's sign-in settings from {issuer} (is {_origin(server_url)} reachable?)")
    names = ("authorization_endpoint", "token_endpoint", "registration_endpoint")
    missing = [n for n in names if not meta.get(n)]
    if missing:
        raise FlaimError(f"Flaim's sign-in settings lack {', '.join(missing)}")
    return Endpoints(
        resource=str(prm.get("resource") or server_url),
        issuer=str(meta.get("issuer") or issuer),
        authorization_endpoint=str(meta["authorization_endpoint"]),
        token_endpoint=str(meta["token_endpoint"]),
        registration_endpoint=str(meta["registration_endpoint"]),
    )


def _oauth_error(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(data, dict) and data.get("error"):
        return f"{data['error']}: {data.get('error_description', '')}".strip(": ")
    return str(data)[:200]


def register_client(http: httpx.Client, endpoints: Endpoints, redirect_uri: str) -> str:
    """RFC 7591 dynamic registration of a public client (no secret) for this loopback redirect URI."""
    body = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": SCOPE,
    }
    try:
        resp = http.post(endpoints.registration_endpoint, json=body)
    except httpx.HTTPError as err:
        raise FlaimError(f"can't reach Flaim's sign-in server ({type(err).__name__})") from err
    if resp.status_code not in (200, 201):
        raise FlaimError(f"Flaim refused FBM's client registration ({resp.status_code}: {_oauth_error(resp)})")
    client_id = (resp.json() or {}).get("client_id")
    if not client_id:
        raise FlaimError("Flaim's client registration returned no client_id")
    return str(client_id)


# ------------------------------------------------------------------ authorization code + PKCE


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge) per RFC 7636."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorization_url(endpoints: Endpoints, client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": SCOPE,
        "resource": endpoints.resource,
    }
    sep = "&" if "?" in endpoints.authorization_endpoint else "?"
    return endpoints.authorization_endpoint + sep + urlencode(query)


def code_from_redirect(text: str, expected_state: str) -> str:
    """The authorization code from where the browser landed: the full redirect URL, its query string, or the bare
    code. A URL must carry this attempt's `state`."""
    raw = (text or "").strip()
    if not raw:
        raise FlaimError("no sign-in response received")
    query = urlparse(raw).query if "://" in raw else raw.lstrip("?")
    params = parse_qs(query)
    if "error" in params:
        detail = params.get("error_description", [""])[0]
        raise FlaimError(f"sign-in was not completed ({params['error'][0]}{': ' + detail if detail else ''})")
    if "code" not in params:
        if "=" not in raw and "/" not in raw:
            return raw  # the bare code
        raise FlaimError("no authorization code in that URL: copy the whole address of the page the browser landed on")
    if params.get("state", [""])[0] != expected_state:
        raise FlaimError("that sign-in response belongs to a different login attempt: run `fbm flaim login` again")
    return params["code"][0]


def _token_request(http: httpx.Client, token_endpoint: str, data: dict[str, str]) -> dict[str, Any]:
    try:
        resp = http.post(token_endpoint, data=data, headers={"Accept": "application/json"})
    except httpx.HTTPError as err:
        raise FlaimError(f"can't reach Flaim's sign-in server ({type(err).__name__})") from err
    if resp.status_code == 200:
        try:
            payload = resp.json()
        except ValueError as err:
            raise FlaimError("Flaim's sign-in server sent an unreadable token response") from err
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise FlaimError("Flaim's sign-in server sent no access token")
        return payload
    error = _oauth_error(resp)
    if resp.status_code in (400, 401) and error.startswith(_LOGIN_ERRORS):
        raise FlaimLoginRequired("the Flaim sign-in expired or was revoked")
    raise FlaimError(f"Flaim's sign-in server answered {resp.status_code} ({error})", status=resp.status_code)


def apply_tokens(state: OAuthState, payload: dict[str, Any], now: float) -> OAuthState:
    """New tokens from a token response; a rotated refresh token restarts the 7-day window."""
    rotated = payload.get("refresh_token")
    return replace(
        state,
        access_token=str(payload["access_token"]),
        expires_at=now + float(payload.get("expires_in") or 3600),
        refresh_token=str(rotated) if rotated else state.refresh_token,
        refresh_issued_at=now if rotated else state.refresh_issued_at,
    )


def exchange_code(http: httpx.Client, state: OAuthState, code: str, verifier: str, now: float) -> OAuthState:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": state.redirect_uri,
        "client_id": state.client_id,
        "code_verifier": verifier,
        "resource": state.resource,
    }
    return apply_tokens(state, _token_request(http, state.token_endpoint, data), now)


def new_state(server_url: str, endpoints: Endpoints, client_id: str, redirect_uri: str) -> OAuthState:
    return OAuthState(
        server_url=server_url,
        resource=endpoints.resource,
        issuer=endpoints.issuer,
        authorization_endpoint=endpoints.authorization_endpoint,
        token_endpoint=endpoints.token_endpoint,
        registration_endpoint=endpoints.registration_endpoint,
        client_id=client_id,
        redirect_uri=redirect_uri,
    )


# ------------------------------------------------------------------ keeping a valid access token


class TokenSession:
    """A valid access token on demand: refreshed shortly before it expires, with every rotated refresh token saved
    at once (a lost rotation would sign FBM out)."""

    def __init__(
        self,
        http: httpx.Client,
        state: OAuthState | None = None,
        *,
        clock: Callable[[], float] = time.time,
        persist: bool = True,
    ):
        state = state if state is not None else load()
        if state is None or not (state.refresh_token or state.access_token):
            raise FlaimLoginRequired("FBM isn't signed in to Flaim")
        self.state = state
        self._http = http
        self._clock = clock
        self._persist = persist

    def bearer(self) -> str:
        if not self.state.access_valid(self._clock()):
            self.refresh()
        return self.state.access_token

    def refresh(self) -> None:
        for _ in range(2):
            used = self.state.refresh_token
            if not used:
                raise FlaimLoginRequired("the Flaim sign-in expired")
            data = {
                "grant_type": "refresh_token",
                "refresh_token": used,
                "client_id": self.state.client_id,
                "resource": self.state.resource,
            }
            try:
                payload = _token_request(self._http, self.state.token_endpoint, data)
            except FlaimLoginRequired:
                stored = load() if self._persist else None
                if stored is None or not stored.refresh_token or stored.refresh_token == used:
                    raise
                self.state = stored  # another FBM process rotated the token first: carry on with its tokens
                if stored.access_valid(self._clock()):
                    return
                continue
            self.state = apply_tokens(self.state, payload, self._clock())
            if self._persist:
                save(self.state)
            return
        raise FlaimLoginRequired("the Flaim sign-in expired")
