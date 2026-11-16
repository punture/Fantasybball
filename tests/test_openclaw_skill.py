"""scripts/render_openclaw_skill.py: skill rendering, path substitution, frontmatter rules, installer helpers, and
the job schedule shared by openclaw/install.sh, openclaw/install.ps1 and the docs."""

from __future__ import annotations

import importlib.util
import json
import re
import shlex
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("render_openclaw_skill", ROOT / "scripts" / "render_openclaw_skill.py")
r = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(r)

POSIX_BIN = "/opt/fbm/.venv/bin/fbm"
WIN_BIN = "C:\\fbm\\.venv\\Scripts\\fbm.exe"
WIN_SPACES_BIN = "C:\\Program Files\\FBM's league\\.venv\\Scripts\\fbm.exe"

# The documented schedule (docs/openclaw.md): name -> (cron ET, delivery, fbm arguments)
EXPECTED_JOBS = {
    "fbm:data-sync": ("0 3 * * *", "none", "data sync"),
    "fbm:health": ("30 3 * * *", "none", "health"),
    "fbm:morning-digest": ("30 8 * * *", "announce", "digest --kind daily"),
    "fbm:weekly-preview": ("0 8 * * 1", "announce", "digest --kind weekly"),
    "fbm:lineup-guard": ("*/15 10-23 * * *", "announce", "alert lineup"),
    "fbm:injury-watch": ("5,20,35,50 10-23 * * *", "announce", "alert injuries"),
}


# ------------------------------------------------------------------ rendering


def test_render_posix_substitutes_the_binary_everywhere():
    files = r.plan(ROOT, POSIX_BIN)
    skills = sorted(p.parent.name for p in (ROOT / "agents" / "skills").glob("*/SKILL.md"))
    roles = sorted(p.stem for p in (ROOT / "agents" / "roles").glob("*.md"))
    assert set(files) == {"SKILL.md"} | {f"references/{s}.md" for s in skills} | {
        f"references/role-{n}.md" for n in roles
    }
    for rel, text in files.items():
        assert r.BIN_TOKEN not in text and "uv run fbm" not in text, rel
        assert r.MARKER in text, rel
    skill = files["SKILL.md"]
    assert f"`{POSIX_BIN} tool <name> key=value ...`" in skill
    assert f"`{POSIX_BIN} tool context_pack`" in skill
    assert "{baseDir}/references/role-reviewer.md" in skill  # left for OpenClaw to resolve
    assert f"`{POSIX_BIN} health`" in files["references/health.md"]


def test_reference_keeps_playbook_and_adds_context():
    files = r.plan(ROOT, POSIX_BIN)
    matchup = files["references/matchup.md"]
    assert matchup.startswith("<!-- fbm-openclaw-skill: rendered from agents/skills/matchup/SKILL.md")
    assert "> When: This week's head-to-head outlook." in matchup and "\n# Matchup\n" in matchup
    assert "\nname: matchup\n" not in matchup  # frontmatter folded into the header
    reviewer = files["references/role-reviewer.md"]
    assert "> Tools this role may use: context_pack, get_proposal," in reviewer and "record_review" in reviewer


@pytest.mark.parametrize(
    ("bin_path", "expected"),
    [
        (POSIX_BIN, POSIX_BIN),
        ("/srv/my league/.venv/bin/fbm", "'/srv/my league/.venv/bin/fbm'"),
        (WIN_BIN, WIN_BIN),
        (WIN_SPACES_BIN, "& 'C:\\Program Files\\FBM''s league\\.venv\\Scripts\\fbm.exe'"),
        ("\\\\server\\share\\fbm.exe", "\\\\server\\share\\fbm.exe"),
    ],
)
def test_command_prefix(bin_path, expected):
    assert r.command_prefix(bin_path) == expected


def test_render_windows_paths_keep_backslashes():
    files = r.plan(ROOT, WIN_SPACES_BIN, platform="windows")
    prefix = "& 'C:\\Program Files\\FBM''s league\\.venv\\Scripts\\fbm.exe'"
    assert f"`{prefix} tool <name> key=value ...`" in files["SKILL.md"]
    assert f"`{prefix} health`" in files["references/health.md"]  # re.sub must not eat the backslashes
    assert r.command_prefix("/opt/x/fbm", platform="windows") == "/opt/x/fbm"
    assert r.command_prefix(WIN_BIN, platform="posix") == "'C:\\fbm\\.venv\\Scripts\\fbm.exe'"


def _code_spans(text: str, start: str) -> list[str]:
    return [s for s in re.findall(r"`([^`]+)`", text) if s.startswith(start)]


def test_skill_quotes_values_safely_for_each_shell():
    # exec runs a shell: a team name like $(...) in double quotes would run on the Gateway host.
    for bin_path, platform in ((POSIX_BIN, "posix"), (WIN_SPACES_BIN, "windows")):
        skill = r.plan(ROOT, bin_path, platform=platform)["SKILL.md"]
        assert not re.search(r"\b[a-z_]+=\"", skill), platform  # no key="value" anywhere
        assert "query='Jalen Brunson'" in skill and "never double quotes" in skill
        assert r.QUOTE_TOKEN not in skill and not r.TOKEN.search(skill)
    posix = r.plan(ROOT, POSIX_BIN)["SKILL.md"]
    example = _code_spans(posix, "owner_quote='yes")
    assert example and shlex.split(example[0]) == ["owner_quote=yes, let's do it"]  # what sh passes to FBM
    windows = r.plan(ROOT, WIN_SPACES_BIN, platform="windows")["SKILL.md"]
    assert "`owner_quote='yes, let''s do it'`" in windows and "’’" in windows
    assert "'\\''" not in windows


def test_render_rejects_unknown_template_tokens():
    with pytest.raises(ValueError, match="__TYPO__"):
        r.render_skill("run __FBM_BIN__ __TYPO__", "/x/fbm")
    assert r.render_skill("run __FBM_BIN__", "/srv/__odd__/fbm") == "run /srv/__odd__/fbm"


def test_skill_frontmatter_follows_openclaw_rules():
    skill = r.plan(ROOT, POSIX_BIN)["SKILL.md"]
    assert skill.startswith("---\n")
    meta, body = r.split_frontmatter(skill)
    assert r.frontmatter_problems(skill) == []
    assert meta["name"] == r.SKILL_NAME == "fbm"
    assert "\n" not in meta["description"] and len(meta["description"]) < r.MAX_DESCRIPTION
    assert json.loads(meta["metadata"]) == {"openclaw": {"os": ["darwin", "linux", "win32"]}}
    assert "requires" not in meta["metadata"]  # don't gate on bins: the Gateway's PATH is minimal
    assert len(body) < 12_000


def test_frontmatter_problems_flags_bad_names_and_descriptions():
    assert r.frontmatter_problems("---\nname: FBM Skill\ndescription: x\n---\n")
    assert r.frontmatter_problems("---\nname: fbm\ndescription: " + "x" * 160 + "\n---\n")
    assert r.frontmatter_problems("---\nname: fbm\n---\n")
    assert r.frontmatter_problems("no frontmatter")
    assert r.frontmatter_problems("---\nname: fbm-2\ndescription: ok\n---\n") == []


def test_skill_routes_core_intents_and_keeps_hard_rules():
    skill = r.plan(ROOT, POSIX_BIN)["SKILL.md"]
    for tool in (
        "simulate_matchup",
        "optimize_lineup",
        "lineup_steps",
        "injuries",
        "free_agents",
        "streaming_plan",
        "evaluate_trade",
        "standings",
        "player_card",
        "list_proposals",
        "record_owner_decision",
        "health_check",
    ):
        assert f"`{tool}" in skill, tool
    playbooks = re.findall(r"\| ([a-z][a-z-]*(?:, [a-z][a-z-]*)*) \|\n", skill)
    assert len(playbooks) >= 12
    for playbook in playbooks:
        for name in playbook.split(", "):
            assert (ROOT / "agents" / "skills" / name / "SKILL.md").is_file(), name
    for rule in ("Numbers come from tools", "never changes the league", "Never call `record_review`", "Untrusted"):
        assert rule in skill
    assert "3500 characters" in skill and "12 short lines" in skill


def test_write_skill_is_idempotent_and_refuses_foreign_folders(tmp_path):
    out = tmp_path / "skills" / "fbm"
    files = r.plan(ROOT, POSIX_BIN)
    r.write_skill(out, files)
    first = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    (out / "references" / "retired-playbook.md").write_text("old", encoding="utf-8")
    r.write_skill(out, files)
    second = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    assert first == second  # deterministic, and stale playbooks are dropped
    assert all(b"\r\n" not in data for data in second.values())

    foreign = tmp_path / "other" / "fbm"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("---\nname: fbm\ndescription: mine\n---\n", encoding="utf-8")
    with pytest.raises(r.SkillConflictError):
        r.write_skill(foreign, files)
    assert r.main(["render", "--bin", POSIX_BIN, "--out", str(foreign)]) == 3
    assert r.main(["render", "--bin", POSIX_BIN, "--out", str(foreign), "--force"]) == 0
    assert r.is_generated(foreign)


# ------------------------------------------------------------------ helpers


@pytest.mark.parametrize(
    ("text", "minimum", "code"),
    [
        ("OpenClaw 2026.11.2 (abc1234)", "2026.6.5", 0),
        ("2026.6.5", "2026.6.5", 0),
        ("openclaw/2026.5.30 linux-x64", "2026.6.5", 1),
        ("2026.1.12-1", "2026.2.17", 1),
        ("command not found", "2026.6.5", 2),
    ],
)
def test_version(text, minimum, code, capsys):
    assert r.main(["version", text, "--min", minimum]) == code
    if code != 2:
        assert re.fullmatch(r"\d{4}\.\d+\.\d+\n", capsys.readouterr().out)


def test_chat_id(tmp_path, capsys):
    settings = tmp_path / "settings.toml"
    settings.write_text("[notify]\ntelegram_chat_id = 123456789\n", encoding="utf-8")
    assert r.main(["chat-id", "--settings", str(settings)]) == 0
    assert capsys.readouterr().out == "123456789\n"
    assert r.main(["chat-id", "--value", " -1001234567890 ", "--settings", str(settings)]) == 0
    assert capsys.readouterr().out == "-1001234567890\n"
    for bad in ("12", "abc", "123; rm -rf /", '1234"'):
        assert r.main(["chat-id", "--value", bad, "--settings", str(tmp_path / "none.toml")]) == 1
    assert r.valid_chat_id("@fbm_alerts") and not r.valid_chat_id("@ab")
    settings.write_text("not = [valid toml", encoding="utf-8")
    assert r.settings_chat_id(settings) == ""


def test_job_ids_reads_every_list_shape():
    jobs = [{"id": "a1", "name": "fbm:health"}, {"jobId": "b2", "name": "fbm:lineup-guard"}, {"id": "c3", "name": "x"}]
    expected = [("a1", "fbm:health"), ("b2", "fbm:lineup-guard")]
    assert r.job_ids(json.dumps({"jobs": jobs})) == expected
    assert r.job_ids(json.dumps(jobs)) == expected
    assert r.job_ids("Gateway: local\n" + json.dumps({"data": jobs, "next": None})) == expected
    assert r.job_ids("[info] gateway ok\n" + json.dumps({"jobs": jobs})) == expected
    assert r.job_ids(json.dumps({"ok": True, "result": {"jobs": jobs}})) == expected  # nested envelope
    assert r.job_ids(json.dumps({"ok": True, "result": {"page": 1, "rows": jobs}})) == expected
    assert r.job_ids("") == [] and r.job_ids("{}") == []
    assert r.job_ids(json.dumps({"jobs": [{"name": "fbm:no-id"}]})) == []


def test_job_ids_cli(tmp_path, capsys):
    listing = tmp_path / "jobs.json"
    listing.write_text("\ufeff" + json.dumps({"jobs": [{"id": "a1", "name": "fbm:health"}]}), encoding="utf-8")
    assert r.main(["job-ids", "--file", str(listing)]) == 0
    assert capsys.readouterr().out == "a1\tfbm:health\n"
    listing.write_text("not json", encoding="utf-8")
    assert r.main(["job-ids", "--file", str(listing)]) == 0  # no JSON at all = no jobs
    listing.write_text("{broken", encoding="utf-8")
    assert r.main(["job-ids", "--file", str(listing)]) == 2


SETTINGS = """# my settings
[owner]
timezone = "America/Chicago"

[notify]
email_enabled = true  # Gmail
recipients = ["owner@example.com"]

[hashtag]
enabled = false
"""


def test_notify_channel_adds_keeps_everything_else_and_backs_up(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text(SETTINGS, encoding="utf-8")
    channels = r.change_notify_channel(path, add="openclaw", chat_id="123456", openclaw_bin="C:\\oc\\openclaw.cmd")
    assert channels == ["openclaw", "email"]
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["notify"] == {
        "channels": ["openclaw", "email"],
        "telegram_chat_id": "123456",
        "openclaw_bin": "C:\\oc\\openclaw.cmd",
        "email_enabled": True,
        "recipients": ["owner@example.com"],
    }
    assert data["owner"] == {"timezone": "America/Chicago"} and data["hashtag"] == {"enabled": False}
    assert "# my settings" in path.read_text(encoding="utf-8")
    assert (tmp_path / "settings.toml.bak").read_text(encoding="utf-8") == SETTINGS

    before = path.read_text(encoding="utf-8")
    assert r.change_notify_channel(path, add="openclaw", chat_id="999") == ["openclaw", "email"]
    assert path.read_text(encoding="utf-8") == before  # idempotent; an existing chat id is never replaced

    assert r.change_notify_channel(path, remove="openclaw") == ["email"]
    assert tomllib.loads(path.read_text(encoding="utf-8"))["notify"]["telegram_chat_id"] == "123456"


def test_notify_channel_multiline_array_missing_table_and_missing_file(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[notify]\nchannels = [\n  "telegram",\n  "email",\n]\n[agent]\nname = "generic"\n', "utf-8")
    assert r.change_notify_channel(path, add="openclaw") == ["openclaw", "telegram", "email"]
    assert tomllib.loads(path.read_text(encoding="utf-8"))["agent"] == {"name": "generic"}

    path.write_text('[owner]\ntimezone = "UTC"\n', encoding="utf-8")
    assert r.change_notify_channel(path, add="openclaw", chat_id="123456") == ["openclaw"]  # email off -> ()
    assert tomllib.loads(path.read_text(encoding="utf-8"))["notify"]["telegram_chat_id"] == "123456"

    missing = tmp_path / "new" / "settings.toml"
    assert r.main(["notify-channel", "--settings", str(missing), "--add", "openclaw"]) == 0
    assert tomllib.loads(missing.read_text(encoding="utf-8")) == {"notify": {"channels": ["openclaw"]}}
    assert r.change_notify_channel(missing, remove="telegram") == ["openclaw"]


# ------------------------------------------------------------------ installers and docs agree


def _bash_jobs() -> dict[str, tuple[str, str, str]]:
    text = (ROOT / "openclaw" / "install.sh").read_text(encoding="utf-8")
    block = re.search(r"^JOBS=\(\n(.*?)^\)", text, flags=re.S | re.M)
    assert block, "JOBS=( ... ) table not found in install.sh"
    jobs = {}
    for line in block.group(1).splitlines():
        name, cron, delivery, timeout, args = line.strip().strip('"').split("|")
        assert timeout.isdigit()
        jobs[name] = (cron, delivery, args)
    return jobs


def _powershell_jobs() -> dict[str, tuple[str, str, str]]:
    text = (ROOT / "openclaw" / "install.ps1").read_text(encoding="utf-8")
    pattern = r"@\{ Name = '([^']+)'; Cron = '([^']+)'; Delivery = '(\w+)'; Timeout = (\d+); Run = '([^']+)' \}"
    return {m[0]: (m[1], m[2], m[4]) for m in re.findall(pattern, text)}


def test_installers_register_the_documented_schedule():
    bash = _bash_jobs()
    assert bash == EXPECTED_JOBS
    assert _powershell_jobs() == EXPECTED_JOBS
    assert re.findall(r'"(fbm:[a-z-]+)\|', (ROOT / "openclaw" / "install.sh").read_text(encoding="utf-8")) == list(
        EXPECTED_JOBS
    )


@pytest.mark.parametrize("doc", ["docs/openclaw.md", "openclaw/README.md"])
def test_docs_list_every_job_with_its_schedule(doc):
    text = (ROOT / doc).read_text(encoding="utf-8")
    rows = {m[0]: m[1] for m in re.findall(r"^\| `(fbm:[a-z-]+)` \| `([^`]+)`", text, flags=re.M)}
    assert rows == {name: cron for name, (cron, _, _) in EXPECTED_JOBS.items()}
    for _, _, args in EXPECTED_JOBS.values():
        assert f"`fbm {args}`" in text


def test_powershell_script_is_structurally_sane():
    # PowerShell can't run here: catch unbalanced brackets and bash-isms in install.ps1 / uninstall.ps1.
    for name in ("install.ps1", "uninstall.ps1"):
        text = (ROOT / "openclaw" / name).read_text(encoding="utf-8")
        code = re.sub(r"<#.*?#>", "", text, flags=re.S)
        code = "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("#"))
        for open_, close in ("{}", "()", "[]"):
            assert code.count(open_) == code.count(close), (name, open_)
        assert "&&" not in code and "??" not in code  # PowerShell 7-only operators
    install = (ROOT / "openclaw" / "install.ps1").read_text(encoding="utf-8")
    assert "--command-argv" not in install  # JSON in native arguments loses its quotes on PowerShell 5.1


def test_bash_installer_is_valid_bash():
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    for name in ("install.sh", "uninstall.sh"):
        subprocess.run([bash, "-n", str(ROOT / "openclaw" / name)], check=True)
