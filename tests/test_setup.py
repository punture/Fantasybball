import tomllib
import zipfile
from pathlib import Path

from fbm import backup as backup_mod
from fbm import setup_wizard as wiz
from fbm.config import CONFIG_DIR, PROJECT_ROOT
from fbm.gm import memory
from fbm.settings import load_settings

EXAMPLE = CONFIG_DIR / "settings.example.toml"
TEMPLATES = PROJECT_ROOT / "templates" / "memory"


class FakeRunner:
    def __init__(self, fail: tuple[str, ...] = (), missing: tuple[str, ...] = ()):
        self.calls: list[list[str]] = []
        self.fail, self.missing = fail, missing

    def __call__(self, cmd):
        args = list(cmd)[3:]  # drop [python, -m, fbm.cli]
        self.calls.append(args)
        if args and args[0] in self.missing:
            return 2, "Error: No such command"
        if args and args[0] in self.fail:
            return 1, "boom"
        return 0, ""


def _which(name):
    return f"/bin/{name}"


def _run(tmp_path: Path, opts: wiz.SetupOptions, runner=None, backfilled=False):
    runner = runner or FakeRunner()
    report = wiz.run_setup(
        opts,
        runner=runner,
        settings_path=tmp_path / "config" / "settings.toml",
        example_path=EXAMPLE,
        memory_dir=tmp_path / "memory",
        template_dir=TEMPLATES,
        league_toml=tmp_path / "config" / "league.toml",
        demo_file=tmp_path / "demo_league.json",
        backfilled=lambda: backfilled,
        which=_which,
    )
    return report, runner


def test_timezone_validation():
    assert wiz.valid_timezone("America/New_York")
    assert wiz.valid_timezone("UTC")
    assert not wiz.valid_timezone("UTC+8")
    assert not wiz.valid_timezone("Mars/Olympus")
    assert not wiz.valid_timezone("../etc/passwd")
    assert wiz.valid_timezone(wiz.detect_timezone())


def test_validate_flags():
    assert wiz.validate(wiz.SetupOptions(agent="codex", timezone="Europe/London", jobs=["health"])) == []
    errors = wiz.validate(
        wiz.SetupOptions(agent="x", timezone="Nowhere", automation="cron", jobs=["bad"], quiet_hours="late")
    )
    assert len(errors) == 5
    assert wiz.parse_jobs("data-sync, health,") == ["data-sync", "health"]
    assert wiz.parse_jobs("none") == []


def test_render_keeps_comments_and_values():
    opts = wiz.SetupOptions(agent="claude-code", gm_model="big", timezone="Asia/Tokyo", jobs=["health"], email=True)
    text = wiz.render_settings(EXAMPLE.read_text(encoding="utf-8"), wiz.merged_settings(None, opts))
    data = tomllib.loads(text)
    assert data["agent"]["name"] == "claude-code"
    assert data["agent"]["gm_model"] == "big"
    assert data["owner"]["timezone"] == "Asia/Tokyo"
    assert data["automation"]["jobs"] == ["health"]
    assert data["notify"]["email_enabled"] is True
    assert data["proposals"]["expiry_hours"] == 18 and "autonomy" not in data
    assert data["yahoo"] == {"route": "auto"} and data["flaim"]["url"] == "https://api.flaim.app/mcp"
    assert data["hashtag"]["enabled"] is False and "channels" not in data["notify"]
    assert data["notify"]["openclaw_bin"] == "openclaw" and data["notify"]["telegram_chat_id"] == ""
    assert "# decisions: a frontier model" in text


def test_write_settings_merges_existing(tmp_path):
    path = tmp_path / "settings.toml"
    wiz.write_settings(wiz.SetupOptions(agent="codex", gm_model="m1", quiet_hours="23:00-07:00"), path, EXAMPLE)
    path.write_text(path.read_text(encoding="utf-8") + '\n[review]\nlegacy = "x"\n', encoding="utf-8")
    result, _ = wiz.write_settings(wiz.SetupOptions(reviewer_model="m2"), path, EXAMPLE)
    assert result.status == wiz.OK
    s = load_settings(path)
    assert (s.agent, s.gm_model, s.reviewer_model, s.quiet_hours) == ("codex", "m1", "m2", "23:00-07:00")
    assert tomllib.loads(path.read_text(encoding="utf-8"))["review"]["legacy"] == "x"
    again, _ = wiz.write_settings(wiz.SetupOptions(), path, EXAMPLE)
    assert again.status == wiz.SKIP


def test_memory_templates(tmp_path):
    created = memory.ensure_memory(tmp_path / "memory", TEMPLATES)
    assert {"strategy.md", "strategy.json", "playbook.md", "managers/README.md", "journal/.gitkeep"} <= set(created)
    assert "Owner rules" in (tmp_path / "memory" / "playbook.md").read_text(encoding="utf-8")
    assert '"punts": []' in (tmp_path / "memory" / "strategy.json").read_text(encoding="utf-8")
    (tmp_path / "memory" / "playbook.md").write_text("mine", encoding="utf-8")
    assert memory.ensure_memory(tmp_path / "memory", TEMPLATES) == []
    assert (tmp_path / "memory" / "playbook.md").read_text(encoding="utf-8") == "mine"


def test_non_interactive_flow(tmp_path):
    opts = wiz.SetupOptions(agent="gemini", timezone="Europe/Paris", automation="engine-only", jobs=["health"])
    report, runner = _run(tmp_path, opts)
    assert not report.failed
    assert ["data", "backfill"] in runner.calls
    assert ["data", "sync"] in runner.calls
    assert ["demo", "build"] in runner.calls
    assert ["agent", "install", "--agent", "gemini"] in runner.calls
    assert ["automation", "install", "--mode", "engine-only", "--jobs", "health"] in runner.calls
    assert runner.calls[-1] == ["health"]
    assert load_settings(tmp_path / "config" / "settings.toml").owner_timezone == "Europe/Paris"
    assert (tmp_path / "memory" / "playbook.md").exists()
    assert any("Open the GM office" in s for s in report.next_steps())


def test_idempotent_and_missing_commands(tmp_path):
    (tmp_path / "demo_league.json").write_text("{}", encoding="utf-8")
    runner = FakeRunner(missing=("agent", "automation"))
    report, runner = _run(tmp_path, wiz.SetupOptions(automation="scheduler", skip_data=True), runner)
    status = {r.step: r.status for r in report.results}
    assert status["NBA data"] == status["Demo league"] == wiz.SKIP
    assert status["Agent files"] == status["Automation"] == wiz.SKIP
    assert not any(c[0] in ("data", "demo") for c in runner.calls)


def test_failures_reported(tmp_path):
    report, _ = _run(tmp_path, wiz.SetupOptions(), FakeRunner(fail=("health", "data")), backfilled=True)
    status = {r.step: r.status for r in report.results}
    assert status["NBA backfill"] == wiz.SKIP
    assert status["NBA sync"] == status["Health"] == wiz.FAIL
    assert report.failed


def test_check_writes_nothing(tmp_path):
    report = wiz.check(
        FakeRunner(),
        settings_path=tmp_path / "settings.toml",
        memory_dir=tmp_path / "memory",
        league_toml=tmp_path / "league.toml",
        which=_which,
    )
    assert report.failed
    assert not (tmp_path / "settings.toml").exists()
    assert not (tmp_path / "memory").exists()


def test_backup_contents(tmp_path):
    (tmp_path / "memory" / "journal").mkdir(parents=True)
    (tmp_path / "memory" / "playbook.md").write_text("p", encoding="utf-8")
    (tmp_path / "memory" / "journal" / "d.md").write_text("j", encoding="utf-8")
    (tmp_path / "memory" / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.toml").write_text("[owner]\n", encoding="utf-8")
    (tmp_path / "config" / "constants.toml").write_text("", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "fbm.db").write_bytes(b"db")
    zpath = backup_mod.backup(root=tmp_path)
    assert zpath.parent == tmp_path / "data" / "backups"
    names = set(zipfile.ZipFile(zpath).namelist())
    assert names == {"memory/playbook.md", "memory/journal/d.md", "config/settings.toml", "data/fbm.db"}


class _Health:
    def __init__(self, healthy, reasons=()):
        self.healthy, self.reasons = healthy, list(reasons)


def _league(tmp_path, key="478.l.30273"):
    path = tmp_path / "config" / "league.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'[league]\nleague_key = "{key}"\n', encoding="utf-8")
    return path


def test_hashtag_flags_validated():
    assert wiz.validate(wiz.SetupOptions(hashtag=True, hashtag_league_id="30273", league_source="yahoo")) == []
    errors = wiz.validate(wiz.SetupOptions(hashtag=True, hashtag_league_id="abc", league_source="demo"))
    assert len(errors) == 2


def test_hashtag_setup_writes_settings_with_yahoo_league_id(tmp_path):
    _league(tmp_path)
    opts = wiz.SetupOptions(league_source="yahoo", hashtag=True, skip_data=True)
    report = wiz.run_setup(
        opts, runner=FakeRunner(), settings_path=tmp_path / "config" / "settings.toml", example_path=EXAMPLE,
        memory_dir=tmp_path / "memory", template_dir=TEMPLATES, league_toml=tmp_path / "config" / "league.toml",
        demo_file=tmp_path / "demo.json", backfilled=lambda: True, which=_which,
        hashtag_health=lambda: _Health(False, ["No Hashtag sign-in stored."]),
    )  # fmt: skip
    s = load_settings(tmp_path / "config" / "settings.toml")
    assert s.hashtag_enabled and s.hashtag_league_id == "30273"
    step = next(r for r in report.results if r.step == "Hashtag")
    assert step.status == wiz.PENDING and "sign-in" in step.detail
    assert any("fbm hashtag login" in n for n in report.next_steps())


def test_hashtag_status_cases(tmp_path):
    league = _league(tmp_path)
    demo = wiz.hashtag_status({"league": {"source": "demo"}, "hashtag": {"enabled": False}}, league)
    assert demo.status == wiz.SKIP
    bad = wiz.hashtag_status({"league": {"source": "demo"}, "hashtag": {"enabled": True}}, league)
    assert bad.status == wiz.FAIL
    off = {"league": {"source": "yahoo"}, "hashtag": {"enabled": False}}
    free = wiz.hashtag_status(off, league)
    assert free.status == wiz.OK and free.detail == "Free ESPN engine (Hashtag off)"
    steps = wiz.SetupReport(settings=off, results=[free]).next_steps()
    assert not any("required" in n.lower() for n in steps)
    assert any("Optional paid upgrade" in n for n in steps)
    no_id = wiz.hashtag_status({"league": {"source": "yahoo"}, "hashtag": {"enabled": True}}, league)
    assert no_id.status == wiz.FAIL and "30273" in no_id.detail
    on = {"league": {"source": "yahoo"}, "hashtag": {"enabled": True, "league_id": "30273"}}
    assert wiz.hashtag_status(on, league, lambda: _Health(True)).status == wiz.OK


# ------------------------------------------------------------------ free mode


def test_free_mode_yahoo_league_without_hashtag_is_ok(tmp_path):
    league = _league(tmp_path)
    opts = wiz.SetupOptions(league_source="yahoo", hashtag=False, yahoo_route="flaim", skip_data=True)
    report, _ = _run(tmp_path, opts)
    status = {r.step: (r.status, r.detail) for r in report.results}
    assert status["Hashtag"] == (wiz.OK, "Free ESPN engine (Hashtag off)")
    assert status["Yahoo league"][0] == wiz.OK and "via Flaim" in status["Yahoo league"][1]
    assert not report.failed and league.exists()
    s = tomllib.loads((tmp_path / "config" / "settings.toml").read_text(encoding="utf-8"))
    assert s["yahoo"]["route"] == "flaim" and s["hashtag"]["enabled"] is False
    steps = report.next_steps()
    assert not any("required" in n.lower() for n in steps)
    assert not any("fbm flaim login" in n for n in steps)  # already connected
    assert any("engine-only" in n and "openclaw/install.sh" in n for n in steps)
    assert any("fbm notify setup" in n for n in steps)


def test_yahoo_route_flag_validated_and_written(tmp_path):
    assert wiz.validate(wiz.SetupOptions(yahoo_route="flaim")) == []
    errors = wiz.validate(wiz.SetupOptions(yahoo_route="oauth"))
    assert errors == ["--yahoo-route must be one of auto, flaim, session, api"]
    path = tmp_path / "settings.toml"
    wiz.write_settings(wiz.SetupOptions(yahoo_route="session"), path, EXAMPLE)
    assert tomllib.loads(path.read_text(encoding="utf-8"))["yahoo"]["route"] == "session"
    wiz.write_settings(wiz.SetupOptions(agent="codex"), path, EXAMPLE)
    assert tomllib.loads(path.read_text(encoding="utf-8"))["yahoo"]["route"] == "session"  # kept


def test_next_steps_follow_the_route():
    def steps(route, approved=False):
        detail = "access approved: follow the Yahoo steps below" if approved else "demo"
        results = [wiz.StepResult("Yahoo league", wiz.PENDING, detail)]
        return wiz.SetupReport(results=results, settings={"yahoo": {"route": route}}).next_steps()

    flaim = steps("flaim")
    assert any("https://flaim.app" in s and "fbm flaim login" in s for s in flaim)
    assert any("fbm league select" in s for s in flaim)
    assert any("official Yahoo API" in s for s in flaim) and not any("fbm auth" in s for s in flaim)
    assert any("fbm yahoo login" in s and "grey area" in s for s in steps("session"))
    assert any("fbm auth" in s for s in steps("api")) and any("fbm auth" in s for s in steps("auto", approved=True))
    auto = steps("auto")
    assert any("--yahoo-route flaim" in s for s in auto) and any("fbm yahoo login" in s for s in auto)
    for route in wiz.YAHOO_ROUTES:
        assert not any("Hashtag" in s for s in steps(route))  # nothing about Hashtag until the league is connected


def test_yahoo_status_mentions_route(tmp_path):
    league = _league(tmp_path)
    assert "Flaim (`fbm flaim login`)" in wiz.yahoo_status("demo", False, league, "flaim")[0].detail
    assert "fbm yahoo login" in wiz.yahoo_status("demo", False, league, "session")[0].detail
    assert "fbm auth" in wiz.yahoo_status("demo", False, league, "api")[0].detail
    ok = wiz.yahoo_status("yahoo", False, league, "session")[0]
    assert ok.status == wiz.OK and "web session" in ok.detail
    assert wiz.yahoo_status("yahoo", False, league)[0].detail == "connected (config/league.toml)"


def test_telegram_flag_writes_notify_channels(tmp_path):
    assert wiz.notify_channels({}, None, True) is None  # no channels key and no --telegram: leave as is
    assert wiz.notify_channels({}, True, None) == ["telegram"]
    assert wiz.notify_channels({"email_enabled": True}, True, None) == ["telegram", "email"]
    assert wiz.notify_channels({"channels": ["openclaw", "email"]}, True, None) == ["telegram", "openclaw", "email"]
    assert wiz.notify_channels({"channels": ["telegram", "email"]}, False, None) == ["email"]
    assert wiz.notify_channels({"channels": ["telegram"]}, None, True) == ["telegram", "email"]
    assert wiz.notify_channels({"channels": ["telegram", "email"]}, None, False) == ["telegram"]
    assert wiz.effective_channels({"email_enabled": True}) == ["email"]
    assert wiz.effective_channels({"email_enabled": True, "channels": []}) == []
    path = tmp_path / "settings.toml"
    wiz.write_settings(wiz.SetupOptions(telegram=True, email=True), path, EXAMPLE)
    notify = tomllib.loads(path.read_text(encoding="utf-8"))["notify"]
    assert notify["channels"] == ["telegram", "email"] and notify["email_enabled"] is True
    wiz.write_settings(wiz.SetupOptions(email=False), path, EXAMPLE)
    notify = tomllib.loads(path.read_text(encoding="utf-8"))["notify"]
    assert notify["channels"] == ["telegram"] and notify["email_enabled"] is False
    wiz.write_settings(wiz.SetupOptions(telegram_chat_id="123456"), path, EXAMPLE)
    assert tomllib.loads(path.read_text(encoding="utf-8"))["notify"]["telegram_chat_id"] == "123456"


def test_notify_next_steps():
    tg = wiz.SetupReport(settings={"notify": {"channels": ["telegram"]}, "automation": {"mode": "engine-only"}})
    steps = tg.next_steps()
    assert any("@BotFather" in s and "fbm notify setup" in s for s in steps)
    assert not any("openclaw/install.sh" in s for s in steps)  # automation already chosen
    none = wiz.SetupReport(settings={}).next_steps()
    assert any("fbm setup --telegram --yes" in s for s in none)


def test_secret_store_step(monkeypatch):
    from keyring.backends import fail

    from fbm import credentials

    monkeypatch.setattr(credentials.keyring, "get_keyring", lambda: fail.Keyring())
    credentials.reset_backend()
    step = wiz.secret_store_status()
    assert step.status == wiz.OK and "credentials.json" in step.detail
    monkeypatch.setenv(credentials.BACKEND_ENV, "keyring")
    assert wiz.secret_store_status().status == wiz.FAIL


def test_setup_flags_reach_options(monkeypatch):
    from typer.testing import CliRunner

    from fbm import cli_setup
    from fbm.cli import app

    seen = {}

    def fake_run_setup(opts, on_step=None):
        seen["opts"] = opts
        return wiz.SetupReport(settings={})

    monkeypatch.setattr(cli_setup.wiz, "run_setup", fake_run_setup)
    monkeypatch.setattr(cli_setup.wiz, "current_values", lambda: {"owner": {"timezone": "Asia/Tokyo"}})
    result = CliRunner().invoke(app, ["setup", "--yes", "--yahoo-route", "Flaim", "--telegram", "--no-hashtag"])
    assert result.exit_code == 0, result.output
    opts = seen["opts"]
    assert (opts.yahoo_route, opts.telegram, opts.hashtag) == ("flaim", True, False)
    bad = CliRunner().invoke(app, ["setup", "--yes", "--yahoo-route", "cookies"])
    assert bad.exit_code == 2 and "--yahoo-route must be one of" in bad.output


def test_engine_only_without_jobs_installs_the_engine_jobs(tmp_path, monkeypatch):
    """`--automation engine-only` with no jobs (docs/free-setup.md one-liner) must not install nothing."""
    from typer.testing import CliRunner

    from fbm import cli_automation
    from fbm.cli import app
    from fbm.settings import Settings

    engine = wiz.engine_only_jobs()
    assert "data-sync" in engine and "health" in engine and "trade-scan" not in engine  # trade-scan is AI-only
    report, runner = _run(tmp_path, wiz.SetupOptions(automation="engine-only", skip_data=True))
    assert ["automation", "install", "--mode", "engine-only", "--jobs", ",".join(engine)] in runner.calls
    s = tomllib.loads((tmp_path / "config" / "settings.toml").read_text(encoding="utf-8"))
    assert s["automation"]["jobs"] == engine
    kept = wiz.merged_settings(None, wiz.SetupOptions(automation="engine-only", jobs=["health"]))
    assert kept["automation"]["jobs"] == ["health"]

    monkeypatch.setattr(cli_automation, "load_settings", lambda: Settings(automation_mode="engine-only"))
    result = CliRunner().invoke(app, ["automation", "install", "--mode", "engine-only", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "nothing to install" not in result.output and "data-sync" in result.output


def test_interactive_route_default_keeps_a_connected_league(monkeypatch):
    """Enter on the route question recommends Flaim to new installs but never moves a connected league off its route
    (that would need a new sign-in); picking another route on a connected league asks to connect it."""
    from fbm import cli_setup

    defaults, asked = {}, []

    def fake_choice(question, options, default):
        defaults[question] = default
        return answers.get(question, default)

    monkeypatch.setattr(cli_setup, "_choice", fake_choice)
    monkeypatch.setattr(cli_setup.typer, "prompt", lambda *a, **k: k.get("default"))
    monkeypatch.setattr(cli_setup.typer, "confirm", lambda *a, **k: False)
    monkeypatch.setattr(cli_setup, "_ask_connect", lambda opts: asked.append(opts.yahoo_route))
    route_q = "How should FBM read your Yahoo league?"
    base = {"owner": {"timezone": "Asia/Tokyo"}, "automation": {"mode": "none"}}

    answers: dict[str, str] = {}
    fresh = wiz.SetupOptions()
    cli_setup._interactive(fresh, {**base, "league": {"source": "demo"}, "yahoo": {"route": "auto"}})
    assert defaults[route_q] == "flaim" and fresh.yahoo_route == "flaim" and asked == ["flaim"]

    asked.clear()
    connected = wiz.SetupOptions()
    cli_setup._interactive(connected, {**base, "league": {"source": "yahoo"}, "yahoo": {"route": "auto"}})
    assert defaults[route_q] == "auto" and connected.yahoo_route == "auto" and asked == []

    answers[route_q] = "flaim"
    moving = wiz.SetupOptions()
    cli_setup._interactive(moving, {**base, "league": {"source": "yahoo"}, "yahoo": {"route": "auto"}})
    assert moving.yahoo_route == "flaim" and asked == ["flaim"]


def test_interactive_connect_questions_follow_the_route(monkeypatch):
    from fbm import cli_setup

    monkeypatch.setattr(cli_setup.typer, "confirm", lambda *a, **k: True)
    flaim = wiz.SetupOptions(yahoo_route="flaim")
    cli_setup._ask_connect(flaim)
    assert flaim.flaim_login and not flaim.yahoo_web_session and not flaim.yahoo_approved
    session = wiz.SetupOptions(yahoo_route="session")
    cli_setup._ask_connect(session)
    assert session.yahoo_web_session and not session.flaim_login
    api = wiz.SetupOptions(yahoo_route="api")
    cli_setup._ask_connect(api)
    assert api.yahoo_approved and not api.yahoo_web_session


def test_flaim_interactive_logs_in_selects_and_switches(monkeypatch, tmp_path):
    from fbm import cli_setup

    calls, written = [], []
    monkeypatch.setattr(cli_setup, "_fbm", lambda *args: calls.append(args) or True)
    monkeypatch.setattr("fbm.config.LEAGUE_CONFIG_PATH", tmp_path / "league.toml")
    monkeypatch.setattr(cli_setup.typer, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli_setup.wiz, "current_values", lambda: {"league": {"source": "demo"}})
    monkeypatch.setattr(cli_setup.wiz, "write_settings", lambda opts: written.append(opts))
    cli_setup._flaim_interactive()
    assert calls == [("flaim", "login"), ("league", "select"), ("settings",), ("snapshot",), ("ids",)]
    assert written[0].league_source == "yahoo"
    calls.clear()
    monkeypatch.setattr(cli_setup, "_fbm", lambda *args: calls.append(args) or False)
    cli_setup._flaim_interactive()
    assert calls == [("flaim", "login")]  # a failed login stops there
