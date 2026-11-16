# FBM for OpenClaw

Connects FBM to your [OpenClaw](https://openclaw.ai) assistant on Telegram:

- **Scheduled posts with no model calls**: OpenClaw command jobs run FBM and post what it prints to your Telegram
  chat (morning digest, Monday preview, lineup and injury alerts). Nothing new means `NO_REPLY`, and nothing is posted.
- **Chat**: the `fbm` skill lets the assistant answer "how's my matchup?", "set today's lineup", "approve P-12" by
  running FBM's tools, under FBM's hard rules (numbers only from tools, never changes your league, never
  self-approves).

Full guide, options and troubleshooting: [docs/openclaw.md](../docs/openclaw.md).

## Quick start

Needs OpenClaw 2026.6.5+ with the Telegram channel connected, its Gateway running on this computer, and FBM set up
(`uv run fbm health` passes).

```bash
openclaw/install.sh --chat-id 123456789      # Linux, macOS, WSL
openclaw/install.sh --dry-run                # preview every change
openclaw/install.sh --uninstall              # remove everything it set up
```

```powershell
powershell -ExecutionPolicy Bypass -File openclaw\install.ps1 -ChatId 123456789   # Windows, native Gateway
powershell -ExecutionPolicy Bypass -File openclaw\install.ps1 -Uninstall
```

Your chat id: send `/whoami` to your bot. If the Gateway runs inside WSL (Windows Hub), install FBM in that WSL
distro and use `install.sh` there.

## Scheduled jobs (US/Eastern)

| Job | Schedule (ET) | Runs | Posts |
|---|---|---|---|
| `fbm:data-sync` | `0 3 * * *` | `fbm data sync` | silent |
| `fbm:health` | `30 3 * * *` | `fbm health` | silent; failures alert you |
| `fbm:morning-digest` | `30 8 * * *` | `fbm digest --kind daily` | daily brief |
| `fbm:weekly-preview` | `0 8 * * 1` | `fbm digest --kind weekly` | Monday preview |
| `fbm:lineup-guard` | `*/15 10-23 * * *` | `fbm alert lineup` | only when something changed |
| `fbm:injury-watch` | `5,20,35,50 10-23 * * *` | `fbm alert injuries` | only on new injury news |

Change one: `--cron morning-digest="0 9 * * *"` (PowerShell: `-Cron 'morning-digest=0 9 * * *'`).

## Files

| Path | What |
|---|---|
| `skills/fbm/SKILL.md.tmpl` | The router skill; `__FBM_BIN__` becomes the absolute path to FBM |
| `install.sh`, `uninstall.sh` | Installer for Linux, macOS and WSL (bash 3.2+) |
| `install.ps1`, `uninstall.ps1` | Installer for Windows with a native Gateway (PowerShell 5.1+) |
| `../scripts/render_openclaw_skill.py` | Renders the skill and FBM's playbooks (`references/`); small helpers both installers share |

The installers only ever replace what they created (the `fbm` skill folder, jobs named `fbm:*`), change OpenClaw
settings through the `openclaw` CLI, and ask before optional changes.
