# Free setup on an always-on computer

This guide sets up the free edition of FBM on a computer that stays on (a desktop, a spare laptop, a mini PC, or a
headless Linux box) so it can help you win your Yahoo Fantasy Basketball league every day:

- **Data:** FBM's own engine on ESPN's public NBA data (projections, G-scores, matchup simulation). No subscription.
- **Yahoo:** read-only, through [Flaim](https://flaim.app), a free hosted connector with its own approved Yahoo
  access. FBM never changes your league.
- **Alerts:** lineup and injury alerts and a daily digest on Telegram, sent directly or through your OpenClaw
  assistant. They tell you the exact moves; you tap them in the Yahoo app.
- **Cost:** nothing. (Optional extras: an AI agent subscription for chatting with the GM; Hashtag Basketball Premium
  as a paid data upgrade. Neither is needed.)

Plan on about an hour the first time, most of it waiting for the one-time NBA data download.

Related guides: [flaim.md](flaim.md) (Flaim in detail), [telegram.md](telegram.md) (alerts),
[openclaw.md](openclaw.md) (OpenClaw), [yahoo.md](yahoo.md) (other Yahoo routes), [commands.md](commands.md) (every
command).

## 1. Install git and uv

uv installs the right Python for FBM by itself, so you only need git and uv. Open a terminal (Windows: PowerShell;
macOS: Terminal; Linux: your shell or an SSH session).

**Windows**

```powershell
winget install --id Git.Git -e
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**macOS**

```sh
xcode-select --install                             # git (or: brew install git)
curl -LsSf https://astral.sh/uv/install.sh | sh    # uv (or: brew install uv)
```

**Linux (desktop or headless)**

```sh
sudo apt install -y git curl     # Debian/Ubuntu; Fedora: sudo dnf install -y git curl
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Close the terminal and open a new one, then check both work: `git --version` and `uv --version`.

## 2. Get FBM

```sh
git clone https://github.com/punture/Fantasybball.git
cd Fantasybball
git checkout <BRANCH>
uv sync
```

Run every later command from this `Fantasybball` folder.

## 3. Run setup (free mode, Flaim route)

```sh
uv run fbm setup
```

It asks one question at a time; press Enter to accept the value in brackets. Suggested answers:

| Question | Answer |
|---|---|
| AI agent | the agent you will chat with (`generic` if none) |
| Models | Enter (your agent's defaults) |
| Time zone | your own, e.g. `America/New_York` (Yahoo game days stay US/Eastern regardless) |
| Schedule | `none` if you will install OpenClaw (step 8A), else `engine-only` |
| How FBM reads Yahoo | `flaim` |
| Telegram alerts | yes |
| Email | no (optional) |

The same thing in one line, without questions:

```sh
uv run fbm setup --yes --yahoo-route flaim --telegram --no-hashtag --no-email --automation engine-only
```

Setup writes `config/settings.toml`, creates your local `memory/` folder, downloads NBA data (step 5), generates
your agent's files and runs a health check. It ends with a report (OK / SKIP / FAIL / PENDING) and numbered next
steps. On a Yahoo league with Hashtag off, the data row reads **"Free ESPN engine (Hashtag off)" — that is the
normal, fully supported state.** Until step 4 is done FBM runs a demo league over real NBA players.

Re-run `uv run fbm setup` any time to change an answer, or `uv run fbm setup --check` to re-verify without changing
anything.

> **Secrets on a machine without a keyring.** FBM keeps tokens in your OS credential store (Windows Credential
> Manager, macOS Keychain, Linux Secret Service). Headless Linux usually has none, so FBM stores them in
> `data/secrets/credentials.json` instead, readable only by your user account and never committed or backed up,
> and prints a one-time note when it creates that file. Once the file exists, FBM keeps using it.
> `uv run fbm status` shows which store is in use.
>
> **Linux desktop with the engine-only schedule:** cron jobs can't reach your desktop keyring, so do the sign-ins in
> steps 4 and 7 with the file store: run `export FBM_CREDENTIALS_BACKEND=file` in that terminal first. From then on
> both your terminal and the scheduled jobs use the file.

## 4. Connect Yahoo through Flaim

1. On any device (your phone is fine), sign up at <https://flaim.app>.
2. At <https://flaim.app/leagues>, connect Yahoo: you sign in on Yahoo's own page and Flaim gets read-only access to
   your leagues. FBM never sees your Yahoo password.
3. Back on the FBM computer:

   ```sh
   uv run fbm flaim login                 # opens a browser to let FBM read your Flaim account
   uv run fbm flaim login --no-browser    # headless: prints a link to open on another device instead
   uv run fbm flaim leagues               # your Yahoo leagues as Flaim sees them
   uv run fbm league select               # pick the league FBM manages (if it didn't ask already)
   uv run fbm setup --league-source yahoo --yes
   ```

4. Read the league once and check it:

   ```sh
   uv run fbm flaim check     # every feed FBM needs (settings, rosters, scoreboard, standings, players, ...)
   uv run fbm settings        # your league's rules; warns loudly if it scores a category FBM can't model
   uv run fbm snapshot        # the first full read of your league
   uv run fbm ids             # Yahoo players FBM couldn't match to NBA data (usually none)
   ```

   If `fbm ids` lists players, add them to `config/yahoo_id_overrides.toml` as it explains. If `fbm settings` or
   `fbm health` reports **League categories FAIL**, your league scores a category FBM doesn't model (it supports
   FG%, FT%, 3PM, PTS, REB, AST, STL, BLK, TO or a subset): FBM ignores it, so weigh it yourself.

More on Flaim, including signing out and what it can and cannot see: [flaim.md](flaim.md).

## 5. NBA data

Setup already ran these (unless you passed `--skip-data`); run them by hand if a step failed:

```sh
uv run fbm data backfill   # one time: last season's games and box scores (player baselines and game-to-game
                           # spread). The longest step: a few minutes or more, with a progress bar.
uv run fbm data sync       # this season: schedule, box scores, injuries, news, ESPN projections. Slower the
                           # first time (catches up the season so far), then quick.
uv run fbm data status     # what is loaded
```

After that, the scheduled `data-sync` job (step 8) keeps it current every night.

## 6. Check everything

```sh
uv run fbm health          # OK / WARN / FAIL with the fix for each; exit code 1 on any FAIL
uv run fbm setup --check   # the setup report again
```

## 7. Telegram alerts

1. In Telegram, open a chat with **@BotFather**, send `/newbot`, and pick a name and a username ending in `bot`.
   BotFather replies with a **token**: treat it like a password.
2. Open a chat with your new bot and send it any message (bots can't message you first).
3. Store the token and your chat id:

   ```sh
   uv run fbm notify setup                     # type the token at its hidden prompt; never paste it in a chat
   uv run fbm notify test --channel telegram   # you should get a test message
   ```

4. See what the alerts look like:

   ```sh
   uv run fbm digest          # today's digest (add --send to deliver it)
   uv run fbm alert lineup    # a lineup alert, or NO_REPLY when there is nothing new
   uv run fbm alert injuries  # an injury alert, or NO_REPLY
   ```

Details and options: [telegram.md](telegram.md).

## 8. Run it 24/7: pick one

**A. OpenClaw** (recommended if you want to chat with your GM from your phone). OpenClaw is a personal AI assistant
that runs on this computer, schedules FBM's digest and alerts, and lets you talk to the GM in your Telegram chat.

```sh
openclaw/install.sh                                            # macOS / Linux
powershell -ExecutionPolicy Bypass -File openclaw\install.ps1  # Windows
```

Follow [openclaw.md](openclaw.md). With OpenClaw, list both `"openclaw"` and `"telegram"` in `[notify] channels`:
FBM tries OpenClaw first and falls back to sending to Telegram directly.

**B. Engine-only schedule** (no AI, nothing else to install). The OS scheduler (Task Scheduler on Windows, launchd on
macOS, cron on Linux) runs FBM's deterministic jobs: data sync, lineup checks, health, and the alert and digest jobs
that `fbm automation jobs` lists.

```sh
uv run fbm automation jobs                              # the job catalog and its US/Eastern times
uv run fbm automation install --mode engine-only --dry-run
uv run fbm automation install --mode engine-only
uv run fbm automation list
```

**Either way, keep the computer awake and signed in.** Scheduled jobs only run while the computer is on.

- Windows: Settings → System → Power → Sleep: *Never*. Lock the screen (Win+L) instead of signing out: FBM's tasks
  run in your session.
- macOS: System Settings → Energy (or Battery → Options): prevent automatic sleeping when the display is off. Stay
  logged in (locking is fine): FBM's launchd jobs run in your session.
- Linux: disable suspend (`sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`).
  cron runs whether or not you are logged in.

## 9. Talking to your GM

- **With OpenClaw:** message it in Telegram ("How's my matchup?", "Who should I pick up?", "Approve P-12").
- **With a coding agent:** open your AI agent (Claude Code, Codex, Copilot, Cursor, Gemini CLI, ...) in the
  `Fantasybball` folder and say **"Open the GM office"**. Setup generated its instructions (`uv run fbm agent
  install` regenerates them).
- **Without AI:** the CLI works on its own: `uv run fbm lineup`, `uv run fbm waiver "<player>"`,
  `uv run fbm proposals list`, and the alerts above.

## 10. Your daily routine

- **Alerts tell you exact moves.** A lineup alert lists the lineup changes to make for that day's games; you tap
  them in the Yahoo Fantasy app. FBM never changes your league itself.
- **Bigger moves are proposals.** Adds/drops, waiver claims and trades come as proposals: an independent review
  checks them, you approve or reject in your own words, FBM re-checks and gives you the exact steps, you make the
  move in Yahoo, then tell the GM (or `uv run fbm proposals done P-12`). The next Yahoo read verifies it.
- **Unapproved proposals expire** (18 hours by default), so stale advice never lingers.
- **Glance at the daily digest**: matchup odds, injuries, and anything that needs you.
- Run `uv run fbm health` now and then, or let the scheduled health job warn you.

## Other ways to reach Yahoo

- **Your own Yahoo sign-in (fallback):** `uv run fbm setup --yahoo-route session --yes`, then
  `uv run fbm yahoo login` (headless: `uv run fbm yahoo login --paste`). It reads your league the way Yahoo's own
  website does. Yahoo doesn't document this route and its Terms of Service restrict automated access without
  permission: a grey area, use it at your own discretion. Details: [yahoo.md](yahoo.md).
- **Official Yahoo API (long term):** apply now at <https://sports.yahoo.com/developer/access/> (approval reportedly
  takes 3-8 weeks). Once approved, follow [yahoo.md](yahoo.md) (create an app, `uv run fbm auth`), then
  `uv run fbm setup --yahoo-route api --yes`. It's the route Yahoo supports, so it's worth having if a third-party
  route ever stops working.

## Updating

```sh
git pull
uv sync
uv run fbm agent install   # refresh your agent's generated files
uv run fbm setup --check
```

Your settings, memory, data and secrets are gitignored, so updates never overwrite them.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `uv` or `git` "not found" right after installing | Open a new terminal (the installer updates PATH for new sessions). |
| "FBM: no usable OS keyring here, so secrets are kept in data/secrets/credentials.json from now on" | Normal on headless Linux, shown once. To use a keyring instead, install and unlock one (GNOME Keyring or KWallet), set `FBM_CREDENTIALS_BACKEND=keyring`, sign in again and delete that file. |
| Linux desktop: things work in your terminal but scheduled jobs say a sign-in or token is missing | cron jobs can't reach the desktop keyring. Re-run the sign-ins once with the file store: `export FBM_CREDENTIALS_BACKEND=file`, then `uv run fbm flaim login` and `uv run fbm notify setup`. Once the file exists every FBM process uses it, terminal and cron alike. |
| "...credentials.json.lock is held by another FBM process" | Another FBM command is saving a token. If none is running, delete that `.lock` file. |
| `fbm flaim check` fails or the snapshot is stale | Check your league still shows at <https://flaim.app/leagues>, then `uv run fbm flaim login` again. `uv run fbm flaim status` shows the connection without secrets. |
| No Telegram messages | Send your bot a message first, re-run `uv run fbm notify setup` (token typo?), then `uv run fbm notify test --channel telegram`. |
| An alert command prints `NO_REPLY` | Nothing new to report since the last alert. That's expected. |
| `fbm health`: ESPN data stale | `uv run fbm data sync`; check the computer didn't sleep. |
| `fbm health`: League categories WARN/FAIL | Your league uses a category FBM can't model (see step 4). FBM ignores it; weigh it yourself. |
| Setup still shows the demo league | `uv run fbm setup --league-source yahoo --yes` after connecting (step 4). |
| Want to start over | Delete `config/settings.toml` and run `uv run fbm setup` (your `memory/` and `data/` stay). |
