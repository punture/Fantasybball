"""`fbm flaim login`: the one-time browser sign-in.

A loopback callback server (RFC 8252) on 127.0.0.1 and a free port catches Flaim's redirect when the browser runs on
this computer. When it runs elsewhere (a phone, or a laptop while FBM lives on a headless box), the final page fails
to load; the owner pastes its address instead. Either way PKCE ties the code to this attempt, and nothing about the
sign-in is printed or logged.
"""

from __future__ import annotations

import secrets
import threading
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import httpx

from fbm.flaim import oauth
from fbm.flaim.errors import FlaimError

CALLBACK_PATH = "/callback"
DONE_PAGE = (
    b"<!doctype html><meta charset='utf-8'><title>FBM</title>"
    b"<p style='font-family:sans-serif'>FBM is signed in to Flaim. You can close this tab.</p>"
)


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _CallbackHTTPServer

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if urlparse(self.path).path != CALLBACK_PATH:
            self.send_error(404)
            return
        self.server.result = self.path
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(DONE_PAGE)
        self.server.done.set()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - never log (the URL holds the code)
        return


class _CallbackHTTPServer(HTTPServer):
    result: str | None = None
    done: threading.Event


class CallbackServer:
    """Waits for the browser's redirect to http://127.0.0.1:<port>/callback."""

    def __init__(self, port: int = 0):
        self._httpd = _CallbackHTTPServer(("127.0.0.1", port), _CallbackHandler)
        self._httpd.done = threading.Event()
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}{CALLBACK_PATH}"

    def start(self) -> CallbackServer:
        self._thread.start()
        return self

    def wait(self, timeout_s: float) -> str | None:
        """The full redirect URL once the browser arrives, else None after `timeout_s`."""
        if not self._httpd.done.wait(timeout_s) or not self._httpd.result:
            return None
        return f"http://127.0.0.1:{self._httpd.server_address[1]}{self._httpd.result}"

    def close(self) -> None:
        if self._thread.is_alive():
            self._httpd.shutdown()
        self._httpd.server_close()


def sign_in(
    http: httpx.Client,
    server_url: str,
    *,
    open_browser: bool = True,
    echo: Callable[[str], None] = print,
    prompt: Callable[[str], str] = input,
    opener: Callable[[str], bool] = webbrowser.open,
    timeout_s: float = 300,
    clock: Callable[[], float] = time.time,
) -> oauth.OAuthState:
    """Discover Flaim's sign-in server, register FBM, get the owner's approval and exchange the code for tokens.
    Returns the new state (the caller saves it)."""
    endpoints = oauth.discover(http, server_url)
    server = CallbackServer().start()
    try:
        client_id = oauth.register_client(http, endpoints, server.redirect_uri)
        verifier, challenge = oauth.pkce_pair()
        expected = secrets.token_urlsafe(24)
        url = oauth.authorization_url(endpoints, client_id, server.redirect_uri, challenge, expected)
        redirect: str | None = None
        if open_browser and opener(url):
            echo("Your browser opened Flaim: sign in (or create a free account), connect Yahoo, then approve FBM.")
            echo(f"If no browser window appeared, open this address yourself:\n{url}")
            redirect = server.wait(timeout_s)
            if redirect is None:
                raise FlaimError(f"no sign-in within {timeout_s:.0f}s: try `fbm flaim login --no-browser`")
        else:
            echo(f"Open this address in any browser, sign in to Flaim and approve FBM:\n{url}")
            echo(
                "If that browser is on another device, its last page won't load: copy that page's full address "
                "from the address bar and paste it here."
            )
            pasted = prompt("Paste the address (or just press Enter if you approved on this computer)").strip()
            redirect = pasted or server.wait(10)
            if not redirect:
                raise FlaimError("no sign-in received: run `fbm flaim login --no-browser` and paste the address")
        code = oauth.code_from_redirect(redirect, expected)
    finally:
        server.close()
    state = oauth.new_state(server_url, endpoints, client_id, server.redirect_uri)
    now = clock()
    state = oauth.exchange_code(http, state, code, verifier, now)
    state.signed_in_at = now
    return state
