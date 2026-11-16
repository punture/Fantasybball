"""Flaim errors. They subclass YahooAPIError, so every caller that already handles a failed Yahoo read (CLI error
messages, snapshot fallbacks, additive syncs) handles a failed Flaim read the same way."""

from __future__ import annotations

from fbm.yahoo.client import YahooAPIError

LOGIN_HINT = "run `fbm flaim login`"


class FlaimError(YahooAPIError):
    def __init__(self, message: str, *, code: str = "", status: int = 0, tool: str = ""):
        RuntimeError.__init__(self, f"Flaim: {message}")
        self.status = status
        self.description = message
        self.path = tool
        self.code = code

    @property
    def is_scope_error(self) -> bool:  # a Yahoo-app permission hint never applies to Flaim
        return False


class FlaimLoginRequired(FlaimError):
    """No usable Flaim sign-in: never signed in, signed out, or the 7-day refresh window lapsed."""

    def __init__(self, reason: str = "not signed in"):
        super().__init__(f"{reason}: {LOGIN_HINT}", code="AUTH_FAILED", status=401)


class FlaimUnavailable(FlaimError):
    """A Yahoo read FBM has no Flaim equivalent for."""

    def __init__(self, what: str):
        super().__init__(f"{what} is not available with the Flaim route (`[yahoo] route` in config/settings.toml)")
