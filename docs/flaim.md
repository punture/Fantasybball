# Reading your Yahoo league through Flaim

[Flaim](https://flaim.app) is a free, read-only service (run by Flaim Fantasy LLC) that connects fantasy leagues to
AI assistants over MCP, the Model Context Protocol. It reaches Yahoo with **its own approved Yahoo app** (read-only
scope), so you don't need your own Yahoo API approval, and FBM doesn't need your Yahoo cookies. FBM talks to Flaim's
MCP server (`https://api.flaim.app/mcp`) directly: no AI chat is involved and no AI subscription is needed.

This is a third way in, next to the two in [yahoo.md](yahoo.md):

| | **Flaim** (`route = "flaim"`) | Yahoo web session (`"session"`) | Official Yahoo API (`"api"`) |
|---|---|---|---|
| Setup | free flaim.app account + `fbm flaim login` | `fbm yahoo login` | approval (weeks) + `fbm auth` |
| Who holds your Yahoo access | Flaim (your Yahoo connection lives in your Flaim account) | your OS credential store | your OS credential store |
| Stays signed in | while FBM reads at least once every 7 days | until Yahoo ends the session | indefinitely |
| Supported | Flaim's public, documented tools | undocumented Yahoo host | Yahoo's official API |

Every route is read-only. FBM never changes your league, and neither can Flaim (it has no write tools at all).

## Privacy: what goes where

- **Flaim** keeps your Yahoo connection (a Yahoo refresh token for its read-only app), stored encrypted on its side,
  and reads your league from Yahoo when FBM asks. Review Flaim's own privacy policy at flaim.app before connecting.
- **FBM** keeps only its Flaim sign-in (an OAuth client id and tokens) in your OS credential store
  (`credentials.FLAIM_OAUTH`): never in a file under this folder, never printed, never in an AI chat. `fbm flaim
  status` shows when it expires, never the tokens.
- League data FBM reads through Flaim (rosters, standings, matchups, transactions) is stored locally like on every
  other route: `data/` (gitignored, never committed).

## Set up

1. Create a free account at <https://flaim.app> and connect Yahoo at <https://flaim.app/leagues> (Yahoo's own
   sign-in page; tick your NBA league).
2. Sign FBM in to Flaim:

   ```sh
   uv run fbm flaim login                  # opens your browser: sign in to Flaim and approve FBM
   uv run fbm flaim login --no-browser     # headless box or remote shell: open the printed address anywhere
   ```

   With `--no-browser`, open the address on any device (your phone works). When you approve, the browser goes to
   `http://127.0.0.1:<port>/callback?...`. On the same computer FBM catches that by itself; on another device the
   page won't load: copy its full address from the address bar and paste it into FBM's prompt.
3. Pick your league (asked automatically; skip the question with `--league-id 466.l.12345`). FBM writes
   `config/league.toml` exactly like `fbm league select`, sets `[yahoo] route = "flaim"` in `config/settings.toml`
   and offers to switch from the demo league (`[league] source = "yahoo"`).
4. Check every feed, then take the first snapshot:

   ```sh
   uv run fbm data sync        # ESPN schedule and box scores (two-week matchups, the makes/attempts fallback)
   uv run fbm flaim check      # each feed OK/WARN/FAIL with counts, unmatched players and every warning
   uv run fbm snapshot
   ```

Other commands: `fbm flaim status` (route, sign-in expiry, league; no secrets), `fbm flaim leagues [--select]`,
`fbm flaim route [auto|flaim|session|api]`, `fbm flaim logout`, `fbm flaim dump`. `fbm league select`, `fbm settings`
and `fbm roster` also go through Flaim while it is the route.

## Staying signed in

Flaim's access tokens last about an hour and its refresh tokens 7 days; every refresh issues a new one. FBM refreshes
on its own whenever it reads, so any scheduled job that reads the league (the `daily-gm` or `lineup-guard`
automation, or a daily `fbm snapshot`) keeps the sign-in alive indefinitely. If FBM doesn't read your league for more than 7 days (computer
off, automation paused), run `fbm flaim login` again. `fbm health` warns two days before the sign-in would lapse and
fails once it has.

## What FBM reads, and what differs from the Yahoo routes

The snapshot (`fbm snapshot`, `data/yahoo_league.json`) has the same meaning on every route: teams, rosters with
Yahoo eligibility, lineup slots and injury status, roster slots, scoring categories, standings, waiver priority,
waiver clear dates, Yahoo-wide % owned, this week's matchups and stat leaders. Flaim's schemas leave some details
open (Flaim sends no week dates, for one), so FBM fills these in itself and says so in `fbm flaim check`:

| What | How FBM gets it on the Flaim route |
|---|---|
| FG%/FT% makes and attempts this week | Yahoo's own: Flaim sends the display-only FGM/A and FTM/A rows (`246/499`) next to FG% and FT%. Only if those rows are missing, or empty (`/`) once the week is under way, does FBM rebuild attempts for your matchup from each day's lineup (read once per past day, then kept) and the ESPN box scores of players in active slots, then apply Yahoo's percentage; a mismatch is listed as a warning. |
| Week dates | Flaim's own dates when it sends them; else the window FBM recorded while that week was current (`data/raw/flaim_weeks.json`); else the Monday–Sunday calendar week. A merged matchup (NBA Cup, All-Star break) is recognized from Yahoo's "games remaining" (more games left than the calendar week holds, or games left after the recorded window ended) and stretched to two weeks. Without that count (or without `fbm data sync`'s NBA schedule) a merged week can't be recognized: check the window `fbm flaim check` prints. |
| Adds used this week | Counted from the league's transactions since the week began. |
| Transaction history (intel, move verification) | Flaim reads by date range: the first sync reads a year back, later syncs from the newest stored move. |
| Pending trades and waiver claims (`fbm offers`) | Your own pending items. A pending trade is shown as incoming or outgoing only when Flaim says who proposed it; otherwise it is listed in `fbm flaim check` warnings. |
| Weekly retro (final results) | Stored for weeks whose dates FBM knows (any week a snapshot saw as current), with Yahoo's makes/attempts. Weeks that ended before FBM's first Flaim snapshot have no known dates and are skipped. |
| `fbm yahoo check` extras (season team stats, matchup history, Yahoo player stats) | Not available on this route: FBM doesn't use them yet. |

Nothing crashes when a feed is missing: tools that need it say "not available with the Flaim route", and the
snapshot keeps the previous one when Flaim can't be read (as on the other routes).

## Limits

- **Rate limit:** 60 MCP requests a minute per user. FBM spaces calls 1.1 s apart; a snapshot takes about 25–45
  calls (one roster per team, plus your and your opponent's past days only when the makes/attempts fallback is needed), so
  under a minute.
- **7-day sign-in window** (above).
- **A small free service:** Flaim is run by one company for free and can change its tools, add limits or stop. Keep
  another route ready (`fbm yahoo login`, or your API application) and switch with `fbm flaim route session`.

## Troubleshooting

| Message | Fix |
|---|---|
| `Flaim: FBM isn't signed in to Flaim: run fbm flaim login` | Run `fbm flaim login`. |
| `the Flaim sign-in expired or was revoked` | More than 7 days without a read, or you removed FBM at flaim.app: `fbm flaim login`. |
| `Flaim lists no Yahoo NBA league for your account` | Connect Yahoo (and your league) at <https://flaim.app/leagues>, then `fbm flaim leagues --select`. |
| `league … isn't connected in Flaim` | The league in `config/league.toml` isn't in your Flaim account: add it at flaim.app/leagues or pick another. |
| Browser doesn't open, or the sign-in times out | `fbm flaim login --no-browser` and paste the final address. |
| `can't reach https://api.flaim.app/mcp` | Network or Flaim outage: FBM keeps serving the last snapshot and retries later. |
| A Yahoo error relayed by Flaim | Usually temporary; if it persists, reconnect Yahoo at flaim.app/leagues. |
| `players without an ESPN id` | `fbm ids`, then add overrides in `config/yahoo_id_overrides.toml` (same as every route). |
| Warnings about missing fields | Run `fbm flaim dump` and share the zip with whoever maintains your FBM: it holds every raw Flaim response so mappings can be fixed against real data. |

`fbm flaim dump [--out DIR]` saves each response as JSON under `data/raw/flaim/<UTC time>/` plus a zip next to it.
The files contain your league's data (team names, managers' nicknames, rosters) but never passwords or tokens;
share them only with someone you trust.

## Switching routes

```sh
uv run fbm flaim route            # show the current route
uv run fbm flaim route session    # back to your Yahoo web session (or: api, auto, flaim)
```

or edit `[yahoo] route` in `config/settings.toml` (`auto` = web session if saved, else the official API; the
default). For one command only: `FBM_YAHOO_ROUTE=flaim uv run fbm snapshot`. `fbm flaim logout` removes FBM's Flaim
sign-in; your Flaim account and its Yahoo connection stay until you remove them at flaim.app.
