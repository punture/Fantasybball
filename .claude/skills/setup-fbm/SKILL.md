---
name: setup-fbm
description: Set up FBM (your AI fantasy basketball GM) from a fresh clone, or change its setup. Use for "set up FBM", "install the GM", "install FBM", "configure my fantasy GM", "connect my Yahoo league", "change my GM models/automation", or whenever config/settings.toml does not exist yet.
---

# Set up FBM

You are installing FBM for the user: an AI front office for one Yahoo Fantasy Basketball league (9-cat H2H).
The deterministic engine is the `fbm` CLI (agents call its tools with `uv run fbm tool`); you only collect choices,
run `fbm setup`, and fix what fails. Works the same in any agent (Copilot, Claude Code, Codex, Cursor, Gemini, or
another agent that can run shell commands).

**Free mode is the default and fully supported:** a real Yahoo league runs on FBM's own ESPN engine, read through
Flaim (free), with alerts on Telegram. Nothing paid is needed. Hashtag Basketball Premium is an optional upgrade only;
never keep the user on the demo league waiting for it. The full owner walkthrough is `docs/free-setup.md`.

## Rules for this conversation

- Ask **ONE question at a time**. Use your ask-user / multiple-choice tool if you have one; otherwise ask in chat
  and offer numbered choices with a recommended default. Keep each explanation to one or two sentences.
- **Never ask for, accept, or echo secrets** (Yahoo Client Secret, Telegram bot token, Gmail app password, tokens,
  cookies). Credentials are typed by the user into the `fbm auth` / `fbm notify setup` prompts or entered in the
  browser windows `fbm flaim login` / `fbm yahoo login` open; FBM stores them in the OS credential store (or, on a
  machine without one such as headless Linux, its private file `data/secrets/credentials.json`, which you never read).
  If you need them run, open a terminal for the user (or tell them the exact command) and wait. If a user pastes a
  secret into chat, tell them to rotate it.
- Never invent model names: suggest examples the user's agent actually offers, and accept whatever they type.
- Re-running is safe: `fbm setup` keeps existing values unless a flag changes them.

## 0. Preflight (no questions)

1. If `config/settings.toml` exists, FBM is already set up: run `uv run fbm setup --check`, show the report, and
   ask what they want to change instead of starting over.
2. Make sure dependencies are installed: `uv sync` (install uv from https://docs.astral.sh/uv/ if missing).
   If `uv` isn't on PATH in your sandbox, say so and give the user the command to run.

## 1. Questions (one at a time, in this order)

1. **Agent + OS.** "Which AI agent are you using?" — copilot (GitHub Copilot app/CLI/VS Code), claude-code,
   codex, cursor, gemini, or generic (anything else that can run shell commands). If it's obvious (you know which agent you are),
   state it and ask them to confirm. Detect the OS yourself if you can; otherwise ask (Windows/macOS/Linux).
2. **Models per role** (names exactly as their agent spells them; empty = agent default):
   - GM (`--gm-model`): their best frontier model — it makes the decisions.
   - Reviewer (`--reviewer-model`): ideally a **different model family** than the GM, so the review is truly
     independent. If their agent only offers one vendor, explain that the reviewer still runs as a fresh-context
     agent with no access to the GM's reasoning, and use the same family.
   - Routine (`--routine-model`): a cheap/fast model for lineup guard and health runs.
   Offer 2–3 examples from the models their agent currently lists (check its model picker / `--help` if you can).
3. **Automation** (`--automation`, then `--jobs`):
   - `none` — they open the agent and ask the GM when they want (always available). Pick this if they will install
     OpenClaw (`openclaw/install.sh`, or `openclaw/install.ps1` on Windows; see `docs/openclaw.md`), which runs the
     schedule and lets them chat with the GM on Telegram.
   - `agent-app` — the agent app's own scheduled automations (only if their agent has them).
   - `scheduler` — the OS scheduler (Task Scheduler / launchd / cron) runs the agent headless (needs its CLI).
   - `engine-only` — the OS scheduler runs deterministic `fbm` jobs, no AI (free; no GM judgment). Recommended for a
     24/7 computer without OpenClaw.
   Jobs: `data-sync`, `lineup-guard`, `daily-gm`, `weekly-retro`, `trade-scan`, `health` (`uv run fbm automation jobs`
   lists the current catalog). Recommend `data-sync,lineup-guard,daily-gm,weekly-retro` for AI modes and
   `data-sync,health` (plus any alert jobs the catalog lists) for engine-only.
4. **Time zone** (`--timezone`, IANA like `America/Chicago`). Propose the detected one; explain Yahoo game days
   are always US/Eastern dates regardless.
5. **Yahoo route** (`--yahoo-route`). FBM only ever reads the league. Offer, recommending (a):
   (a) `flaim` — **recommended**: Flaim (https://flaim.app), a free hosted read-only connector with its own approved
   Yahoo access. The user signs up at flaim.app, connects Yahoo at https://flaim.app/leagues (Yahoo sign-in in their
   browser; never ask for the password), then runs `uv run fbm flaim login`. See `docs/flaim.md`.
   (b) `session` — their own Yahoo browser sign-in (`uv run fbm yahoo login`): works now, but Yahoo doesn't document
   it and its Terms of Service restrict automated access (a grey area; see docs/yahoo.md).
   (c) `api` — their own approved Yahoo developer app (`uv run fbm auth`): the supported long-term route, but approval
   at https://sports.yahoo.com/developer/access/ takes 3–8 weeks. Suggest applying now either way.
   Until a route is connected FBM runs a **demo league** (simulated draft over real NBA players), so everything works
   in the meantime — but don't stop there: connect the real league in step 3 below.
6. **Alerts and preferences.**
   - Telegram alerts (`--telegram`, recommended): lineup and injury alerts plus a daily digest. They create a free bot
     with @BotFather and type its token into `uv run fbm notify setup` (see `docs/telegram.md`). Email via Gmail is
     also available (`--email`); quiet hours `--quiet-hours 23:00-07:00`.
   - Proposal expiry (`--expiry-hours`, default 18; `[proposals] expiry_hours`). FBM never changes the league —
     lineups are recommendations and moves are proposals the owner approves — so there is no autonomy to choose.
   - Risk appetite (conservative / balanced / aggressive): not a setup flag — journal it later with
     `journal_append` (kind `owner`) for the season-strategy run.
7. **Data layer (no question needed).** FBM's free ESPN engine is the default (`--no-hashtag`). Only if the user
   says they already have Hashtag Basketball Premium (paid, Patreon) with the Yahoo league imported, offer it as an
   optional upgrade (step 3 below); with it on, FBM pauses suggestions whenever Hashtag can't be read.

## 2. Run setup

Summarize their answers in one short list, then run (one line):

```
uv run fbm setup --yes --agent <agent> --gm-model "<m>" --reviewer-model "<m>" --routine-model "<m>" --timezone <tz> --automation <mode> --jobs <a,b> --yahoo-route flaim --telegram --no-email --no-hashtag --expiry-hours 18 [--quiet-hours 23:00-07:00] [--skip-data]
```

It reports each step OK / SKIP / FAIL / PENDING: prerequisites → secret store → config/settings.toml → memory/ from
templates → NBA data (one-time prior-season backfill + sync; several minutes the first time) → demo league →
`fbm agent install` → `fbm automation install` → Yahoo status → data layer (`Free ESPN engine (Hashtag off)` is OK
for a real league; SKIP on the demo) → `fbm health`. Show the report table to the user.

## 3. Fix and finish

- For each FAIL: read the detail, fix it (or give the user the exact command), then `uv run fbm setup --check`.
- **Connect the real league now** (don't leave them on the demo):
  - Flaim: once they have signed up and connected Yahoo at flaim.app, the user runs `uv run fbm flaim login` in a
    terminal (it opens a browser window; they sign in there). Then you run `uv run fbm flaim check`,
    `uv run fbm league select` if no league is selected yet, `uv run fbm setup --yes --league-source yahoo`,
    `uv run fbm snapshot` and `uv run fbm ids`.
  - Yahoo sign-in: the user runs `uv run fbm yahoo login` in a terminal (they sign in in the browser window it
    opens; never ask for their Yahoo password), picks the league and accepts the switch. Then you run
    `uv run fbm yahoo check` and show which feeds work.
  - Yahoo API approved: the user runs `uv run fbm auth` (types Client ID/Secret into its prompts; they first create a
    NEW Yahoo app with Fantasy Sports Read and redirect URI `https://localhost:8080`, and submit its Client ID at
    https://sports.yahoo.com/developer/application-confirmation/), then you run `uv run fbm league select`,
    `uv run fbm settings`, `uv run fbm setup --yes --league-source yahoo --yahoo-route api`, `uv run fbm snapshot`,
    `uv run fbm ids`.
- Then `uv run fbm settings` and `uv run fbm health`: if "League categories" warns or fails, tell the user exactly
  which of their league's categories FBM can't model (FBM ignores them; they must weigh them themselves).
- Telegram / email chosen: ask the user to run `uv run fbm notify setup` in a terminal (they type the bot token /
  Gmail app password; never in chat), then you run `uv run fbm notify test --channel telegram`.
- 24/7 computer: either the user installs OpenClaw (`openclaw/install.sh`, or `openclaw/install.ps1` on Windows; see
  `docs/openclaw.md`), or you run `uv run fbm automation install --mode engine-only` (if not done by setup).
- Hashtag (optional paid upgrade, only if they have it, after Yahoo is connected): run
  `uv run fbm setup --yes --hashtag` (league id defaults to the Yahoo league id; `--hashtag-league-id <id>` to
  override). The user runs `uv run fbm hashtag login` in a terminal (they sign in to Hashtag in the browser window it
  opens; FBM never sees the password). Then you run `uv run fbm hashtag check` and `uv run fbm hashtag import`; show
  the result. Unmatched players → `config/hashtag_id_overrides.toml`. Kill switch: `FBM_HASHTAG_ENABLED=0`.
- If the generated GM agent/skills aren't loaded in this session yet, tell the user to **restart the agent in this
  folder**, then ask: "Open the GM office". Otherwise run the GM's first steps now:
  `uv run fbm tool context_pack`, then `uv run fbm tool health_check`, and summarize what is configured and what is
  pending.
