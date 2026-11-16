"""Flaim: read the Yahoo league through Flaim's free, read-only MCP server (`[yahoo] route = "flaim"`).

Flaim (https://flaim.app) connects Yahoo with its own approved Yahoo app (read-only scope), so the owner needs no
Yahoo API approval of their own. FBM talks to it as a small synchronous MCP client:

- `oauth`: OAuth 2.1 sign-in (discovery, dynamic client registration, PKCE, refresh); tokens in the OS credential store.
- `login`: the one-time browser sign-in (`fbm flaim login`).
- `client`: JSON-RPC over streamable HTTP (`call_tool`), paced and retried.
- `reads`: Flaim payloads -> FBM's Yahoo read types (`FlaimLeague` is what the fbm.yahoo fetch functions accept).
- `banked`: week-to-date makes/attempts rebuilt from daily lineups and ESPN box scores (pure).
- `league`: the league snapshot (same LeagueState semantics as the Yahoo XML path).
- `route`: which way FBM reads Yahoo (auto | flaim | session | api).
- `check`: `fbm flaim check` / `fbm flaim dump`.

FBM never writes to Yahoo, and neither can Flaim.
"""
