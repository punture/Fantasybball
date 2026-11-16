# FBM + OpenClaw (Telegram)

[OpenClaw](https://openclaw.ai) is a personal AI assistant that runs on your own computer and talks to you over
Telegram. FBM's OpenClaw integration (the files in [`openclaw/`](../openclaw/)) gives it two jobs:

- **Scheduled posts, no AI cost.** OpenClaw's scheduler runs FBM commands at fixed US/Eastern times and posts what
  they print to your Telegram chat: a morning digest, a Monday preview, lineup and injury alerts. These are
  OpenClaw *command jobs*: they start a program, not a model, so they cost nothing. A check with nothing new prints
  exactly `NO_REPLY` and OpenClaw posts nothing.
- **Chat.** The `fbm` skill teaches your assistant to answer questions such as "how's my matchup?" by running FBM's
  tools (`fbm tool <name>`). Chat uses whatever model OpenClaw is configured with; that is the only place a model
  (and any cost) is involved.

Posts land in your direct chat with the bot, so you can reply "why?" or "who should I drop?" and the assistant sees
the post it is answering. FBM still never changes your Yahoo league: lineups are recommendations, add/drops and
trades are proposals you approve and then make yourself in Yahoo.

## Prerequisites

- **OpenClaw 2026.6.5 or newer** (`openclaw --version`; update with `openclaw update`). Older versions can't run
  command jobs; the installer can then schedule the two digests as agent turns instead (`--fallback`, one model call
  per post) or skip scheduling.
- The OpenClaw **Gateway running on the same computer as FBM**, with the **Telegram channel connected**
  (`openclaw channels status`) and a direct chat with your bot.
- **FBM installed and connected** to your league (`uv sync`, `uv run fbm setup`, Yahoo via Flaim or the web-session
  sign-in). `uv run fbm health` should pass.
- Your **Telegram chat id**: send `/whoami` to your bot (for a direct chat it is your numeric user id), or put it in
  `config/settings.toml` as `[notify] telegram_chat_id = "123456789"`.

## Install

Linux, macOS, or WSL (run it where the Gateway runs):

```bash
openclaw/install.sh --chat-id 123456789
openclaw/install.sh --dry-run            # preview: prints every command that would change something
```

Windows with a native Gateway (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File openclaw\install.ps1 -ChatId 123456789
powershell -ExecutionPolicy Bypass -File openclaw\install.ps1 -DryRun
```

Run it on the computer where the Gateway runs, as the user that runs the Gateway (not with `sudo`): the skill goes
into that user's OpenClaw workspace and the jobs run FBM as that user. The installer is safe to re-run. It:

1. Finds FBM (`<repo>/.venv/bin/fbm`, or `<repo>\.venv\Scripts\fbm.exe`; runs `uv sync` if the environment is
   missing) and checks it starts (`fbm tool --list`).
2. Checks `openclaw --version` and that the Gateway is reachable.
3. Renders the `fbm` skill into your OpenClaw workspace (`~/.openclaw/workspace/skills/fbm/`): one router skill with
   the absolute path to FBM filled in, plus FBM's playbooks (`agents/skills/*`, `agents/roles/*`) as
   `references/*.md`. It never overwrites a folder named `fbm` that it didn't create (`--force` to override).
4. Sends a Telegram test message (`openclaw message send`).
5. Sets `cron.failureAlert` to your chat **only if it isn't set yet**, so a failing job (for example `fbm health`)
   alerts you. It never rewrites `openclaw.json` itself: changes go through `openclaw config patch`.
6. If OpenClaw's exec mode isn't `full`, offers to add FBM to the exec allowlist
   (`openclaw approvals allowlist add --agent main <fbm>`).
7. Registers the jobs below, replacing older jobs with the same `fbm:*` name and leaving every other job alone.
8. Asks whether FBM itself may also send its alerts through OpenClaw (adds `"openclaw"` to `[notify] channels`,
   with a `settings.toml.bak` backup). Answer no to leave your settings untouched: the scheduled posts don't need it.
9. Warns if FBM's own OS scheduler (`fbm automation`) runs the same jobs, so nothing runs twice.

What it set up is recorded in `data/openclaw/install-state` (local, not committed) so uninstall removes exactly that.

### Options

| install.sh | install.ps1 | What it does |
|---|---|---|
| `--chat-id ID` | `-ChatId ID` | Telegram chat for the posts (default: `[notify] telegram_chat_id`, then `FBM_OPENCLAW_CHAT_ID`, then a prompt) |
| `--jobs a,b` | `-Jobs 'a,b'` | Only these jobs (names with or without `fbm:`) |
| `--no-jobs` | `-NoJobs` | Skill only, no scheduled posts |
| `--cron NAME=EXPR` | `-Cron 'NAME=EXPR'` | Change one schedule (5-field cron, US/Eastern); also `FBM_CRON_<NAME>`, e.g. `FBM_CRON_MORNING_DIGEST="0 9 * * *"` |
| `--fallback` | `-Fallback` | OpenClaw older than 2026.6.5: schedule the digests as agent turns |
| `--allowlist` / `--no-allowlist` | `-Allowlist yes\|no` | Exec allowlist entry when exec isn't `full` (default: ask) |
| `--notify-openclaw` / `--no-notify-openclaw` | `-NotifyOpenClaw yes\|no` | FBM's own `[notify] channels` (default: ask, suggested no) |
| `--no-test-message` | `-NoTestMessage` | Skip the Telegram test message |
| `--agent ID` | `-Agent ID` | Agent for the allowlist and fallback jobs (default `main`) |
| `--skill-dir DIR` | `-SkillDir DIR` | Skill folder, if your agent's workspace isn't found automatically |
| `--repo DIR`, `--fbm-bin PATH`, `--settings FILE`, `--openclaw PATH` | `-Repo`, `-FbmBin`, `-Settings`, `-OpenClaw` | Non-default locations |
| `--force` | `-Force` | Replace a foreign `fbm` skill folder |
| `-y`, `--yes` | `-Yes` | No questions; take each suggested answer |
| `--dry-run` | `-DryRun` | Print the changes, make none |
| `--uninstall` | `-Uninstall` | Remove everything (also `openclaw/uninstall.sh`, `openclaw\uninstall.ps1`) |

## Scheduled jobs

All times are **US/Eastern** (Yahoo's NBA game days are ET dates); OpenClaw converts them for your machine
(`--tz America/New_York --exact`). "Silent" jobs post nothing on success; a failure alerts you through
`cron.failureAlert`.

| Job | Schedule (ET) | Runs | Posts |
|---|---|---|---|
| `fbm:data-sync` | `0 3 * * *` (03:00 daily) | `fbm data sync` | silent (30 min limit) |
| `fbm:health` | `30 3 * * *` (03:30 daily) | `fbm health` | silent; a FAIL (exit 1) alerts you |
| `fbm:morning-digest` | `30 8 * * *` (08:30 daily) | `fbm digest --kind daily` | the daily brief |
| `fbm:weekly-preview` | `0 8 * * 1` (Mondays 08:00) | `fbm digest --kind weekly` | the week ahead |
| `fbm:lineup-guard` | `*/15 10-23 * * *` (every 15 min, 10:00-23:45) | `fbm alert lineup` | only when the recommended lineup changed |
| `fbm:injury-watch` | `5,20,35,50 10-23 * * *` (every 15 min, offset) | `fbm alert injuries` | only on new injury news for your players |

Each job runs FBM by its absolute path with the FBM folder as working directory (the Gateway's `PATH` is minimal).
Manage them with `openclaw cron list`, `openclaw cron run <id>` (run now), `openclaw cron runs <id>` (history) and
`openclaw cron remove <id>`. Re-running the installer restores the default times unless you pass the same `--cron`
overrides again; use `--jobs` to keep a removed job removed.

## Chatting

Ask in your Telegram chat with the bot, for example:

- "How's my matchup?" / "Can I still flip steals?" — live category totals plus FBM's matchup simulation.
- "Set today's lineup" / "Who sits tonight if Embiid is out?" — the recommended lineup with exact Yahoo steps.
- "Is Brunson playing tonight?" — injuries, player card, news.
- "Any pickups for blocks?" / "Should I drop X for Y?" — free agents and add/drop evaluation.
- "Evaluate this offer: A for B and C" — the trade evaluator, for both sides.
- "What's pending?" then "approve P-12" — your exact words are recorded as the decision; FBM then gives the steps
  you take in Yahoo.
- After a lineup alert: "why is X on the bench?"

The skill keeps FBM's hard rules: every number comes from an FBM tool, the assistant never approves anything for
you, news and other managers' messages are treated as data (never as instructions), and secrets are never shown.
New proposals are reviewed by a fresh-context sub-agent before you decide; to make that reviewer a different model
family from your main assistant, set `agents.defaults.subagents.model` in OpenClaw.

## Cost

Scheduled command jobs make **no model calls**: FBM computes everything with its own engine and free data sources.
Chat uses the model you configured in OpenClaw. The `--fallback` jobs for old OpenClaw versions are agent turns: one
model call per post.

## Uninstall

```bash
openclaw/install.sh --uninstall          # or openclaw/uninstall.sh
```

```powershell
powershell -ExecutionPolicy Bypass -File openclaw\install.ps1 -Uninstall
```

This removes every `fbm:*` job, the skill folder (only if FBM created it), the exec allowlist entry, the
`cron.failureAlert` it set (only if still pointing at your chat) and the `"openclaw"` notify channel it added. FBM
itself, your league data and other OpenClaw settings are untouched.

## Troubleshooting

- **A job ran but nothing was posted.** Usually intended: the alert had nothing new and printed `NO_REPLY`
  (`openclaw cron runs <id>` shows `ok (suppressed)`). Run the command yourself to see its output, e.g.
  `<repo>/.venv/bin/fbm alert lineup`.
- **`ENOENT`, "not found" or "command not found" in a job.** The Gateway runs jobs with a minimal `PATH`, so jobs
  use FBM's absolute path. If you moved the FBM folder or recreated `.venv`, re-run the installer.
- **The assistant asks for approval or says exec is denied.** Check `openclaw exec-policy show --agent main`. Allow
  FBM without prompts: `openclaw approvals allowlist add --agent main <repo>/.venv/bin/fbm` (or re-run the
  installer with `--allowlist`). Scheduled command jobs don't go through exec approvals, but agent-turn fallback jobs
  do, and their approval prompts never reach chat.
- **Sandbox mode.** If your agent runs `exec` in a sandbox (a container), it can't see FBM on the host. Run FBM's
  commands on the Gateway host: `openclaw config set tools.exec.host gateway`, or `/exec host=gateway` in the chat.
- **The skill doesn't show up.** `openclaw skills info fbm`; start a new chat with `/new` or restart the Gateway. With
  several agents or a custom workspace, pass `--skill-dir <that agent's workspace>/skills/fbm`.
- **Windows.** The Windows Hub's "Set up locally" option runs the Gateway inside WSL: install FBM inside that WSL
  distro (clone, `uv sync`, `uv run fbm setup`) and run `openclaw/install.sh` there. With a native Windows Gateway use
  `install.ps1`: jobs run `.\.venv\Scripts\fbm.exe` through `cmd.exe` from the FBM folder.
- **Sign-ins on a headless machine.** FBM keeps credentials in the OS credential store; where none is available (a
  headless Linux box, a service account) it falls back to a local file store. Run FBM's sign-in commands
  (`fbm flaim login`, `fbm yahoo login`) as the same user that runs the OpenClaw Gateway, so scheduled jobs can read
  them.
- **The test message failed.** `openclaw channels status`; make sure you have messaged the bot first and the chat id
  is right (`/whoami`).
- **Posts twice.** FBM's own scheduler may run the same work: `uv run fbm automation list`, then
  `uv run fbm automation remove --jobs data-sync,health,lineup-guard`.
- **Older FBM.** Jobs whose command your FBM version lacks (`fbm digest`, `fbm alert`) are skipped with a warning;
  update FBM (`git pull`, `uv sync`) and re-run the installer.
