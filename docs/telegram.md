# Telegram alerts (free, no AI needed)

FBM can message you on Telegram around the clock from the computer it runs on (Windows, macOS or Linux, headless
included). Everything is computed by FBM's engine, so no AI agent or paid service is involved:

| Message | When (engine-only automation) | Command |
|---|---|---|
| Daily brief: verdict, today's lineup steps, matchup odds, injuries, best stream, waiver target, pickups, your queue, health | daily-gm, 04:00 ET | `fbm digest --send` |
| Weekly preview: new opponent, per-category odds, games both sides, the week's streaming plan, adds left | weekly-retro, Monday 05:00 ET | `fbm digest --kind weekly --send` |
| Lineup alert: a starter ruled out or a game-time decision, a bench player who should start, a changed recommendation | lineup-guard, every 15 min 10:00-23:45 ET, about 90 min before the first affected tip-off | `fbm alert lineup --send` |
| Injury update: status changes on your roster and your opponent's, "next man up" free agents | lineup-guard, every 15 min | `fbm alert injuries --send` |
| Health problem: the checks that FAIL (e.g. a lapsed Flaim sign-in) and how to fix each | health, 03:30 ET, only on a FAIL, at most once a day per problem | `fbm health --notify-on-fail` |

FBM never changes your league: alerts carry the exact steps for you to make in Yahoo. Times are shown in your time
zone (`[owner] timezone`); Yahoo game days are US/Eastern dates.

## 1. Create your bot (BotFather)

1. In Telegram, open a chat with **@BotFather** (the official one, with the blue check mark).
2. Send `/newbot`, pick a display name, then a username that ends in `bot` (for example `my_fbm_gm_bot`).
3. BotFather replies with a **token** like `123456789:AA...`. Treat it like a password: anyone with it controls
   the bot. Don't paste it into chats, files or settings.
4. Open your new bot and press **Start** (or send it any message). A bot can't write to you before you do.

If you already have a bot for OpenClaw, you can reuse it (see "Channels" below).

## 2. Find your chat id

Your chat id is a number (groups and channels start with `-`). Pick one way:

- Send your bot a message, then open `https://api.telegram.org/bot<token>/getUpdates` in a browser (put your token
  in place of `<token>`) and read `"chat":{"id":123456789,...}`.
- If OpenClaw already talks to you through this bot, its pairing message shows your numeric Telegram id.
- `fbm notify setup --detect-chat-id` makes that one getUpdates call for you. If OpenClaw (or another program) is
  polling the same bot, it may see a one-off conflict: stop it first or use one of the other ways.

FBM itself only ever calls `sendMessage`; it never polls your bot.

## 3. Set it up

```
uv run fbm notify setup
```

It asks for the channels (in order), the bot token (hidden input; Enter keeps a stored one) and your chat id, then:

- stores the token in your OS credential store (Windows Credential Manager, macOS Keychain, Linux Secret Service),
  or, on a machine without one, in FBM's private file `data/secrets/credentials.json`,
- writes `[notify] channels` and `telegram_chat_id` into `config/settings.toml` (never the token).

Non-interactive options: `--channels telegram,email`, `--chat-id 123456789`, `--openclaw-bin /path/to/openclaw`.

Then check it:

```
uv run fbm notify test                      # every enabled channel
uv run fbm notify test --channel telegram   # one channel, no fallback
uv run fbm health                           # Telegram / OpenClaw readiness (never shows secrets)
```

## 4. Schedule it

```
uv run fbm automation install --mode engine-only --jobs data-sync,lineup-guard,daily-gm,weekly-retro,health
uv run fbm automation jobs      # the schedule in ET and in your time zone
```

This uses the OS scheduler (Task Scheduler, launchd or cron) and keeps working with no AI agent. The scheduler runs
on the computer's own clock: the install converts the US/Eastern times to it, whatever your `[owner] timezone`. With
an AI agent (`--mode scheduler`) lineup-guard stays hourly; the 15-minute alert schedule is for engine-only mode.

## Channels

`[notify] channels` is an ordered list:

- `telegram`: FBM sends directly through the Telegram Bot API with your bot token.
- `openclaw`: FBM runs your OpenClaw assistant's CLI:
  `openclaw message send --channel telegram --target <chat id> --message <text>` (plus `--silent` for non-urgent
  messages). Set `[notify] openclaw_bin` if `openclaw` isn't on the scheduler's PATH; FBM also looks in common
  install folders (npm global, nvm, Homebrew, `~/.local/bin`, `%APPDATA%\npm`).
- `email`: Gmail (`fbm notify setup` asks for an App Password).

With both `openclaw` and `telegram` enabled, FBM tries OpenClaw first and falls back to direct Telegram if OpenClaw
fails, so each message arrives once. Email is separate: it gets its own copy. Every attempt is in
`fbm notify outbox` (channel, status, detail).

You can also let OpenClaw run the commands itself (for example from its own schedule): `fbm digest`,
`fbm alert lineup` and `fbm alert injuries` print the message on stdout (FBM's Markdown subset, at most 3500
characters) and an alert prints exactly `NO_REPLY` when there is nothing new. Without `--send` FBM delivers nothing
itself. Exit code 0 means success (NO_REPLY included); 1 means a failure, with the reason on stderr.

## Quiet hours

`[notify] quiet_hours = "23:00-07:00"` (your time zone). During quiet hours, non-urgent messages (the digests) are
held, then delivered by the next send after quiet hours end (at most 12 hours later). The 15-minute lineup-guard
job releases them even when it has nothing new to say; `fbm notify flush` does it by hand. Lineup alerts, injury updates and approval requests are urgent: they are never held. Non-urgent
messages arrive silently (no notification sound).

## Troubleshooting

- **"Telegram rejected the bot token (401)"**: the token is wrong or was revoked in BotFather. Run
  `fbm notify setup` again and paste the current token.
- **"chat not found" / "bot was blocked" (400/403)**: check `telegram_chat_id`, and open your bot and press Start
  (or unblock it).
- **"rate limit (429)"**: Telegram asked FBM to slow down. FBM waits and retries short delays itself.
- **"OpenClaw CLI 'openclaw' not found"**: set `[notify] openclaw_bin` to its full path (`which openclaw` /
  `where openclaw`). With `telegram` also enabled, messages still arrive through the fallback meanwhile.
- **Nothing arrives at night**: quiet hours hold the digests; see `fbm notify outbox` (status `held`, later
  `released`).
- **"Credential store unavailable"**, or scheduled jobs say the bot token is missing while your terminal works: the
  scheduler can't reach your desktop keyring (common on Linux with cron). Use FBM's file store instead: run
  `export FBM_CREDENTIALS_BACKEND=file`, then `fbm notify setup` again (and `fbm flaim login` if you use Flaim).
  Once `data/secrets/credentials.json` exists every FBM process uses it, terminal and scheduler alike. See the
  Troubleshooting table in [free-setup.md](free-setup.md).
- **An alert keeps saying NO_REPLY**: that's normal unless something is actionable and new. A lineup alert only
  fires within about 90 minutes of the first affected tip-off, once per recommendation per ET game day.
