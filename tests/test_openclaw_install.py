"""openclaw/install.sh end to end, against fake `openclaw` and `fbm` programs (no network, no real OpenClaw).

Each test copies the parts of the repo the installer needs into a temp folder, so nothing touches the real checkout,
the real ~/.openclaw or config/settings.toml.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(sys.platform == "win32" or BASH is None, reason="bash installer (Linux/macOS/WSL)")

FAKE_OPENCLAW = """#!{python}
import json, os, sys
a = sys.argv[1:]
rec = {{"argv": a}}
if "--file" in a:
    rec["file"] = open(a[a.index("--file") + 1], encoding="utf-8").read()
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(rec) + "\\n")
if a == ["--version"]:
    print(os.environ.get("FAKE_OC_VERSION", "OpenClaw 2026.11.2 (abc1234)"))
elif a[:2] == ["cron", "list"]:
    print(os.environ.get("FAKE_CRON_LIST", ""))
elif a[:2] == ["config", "get"]:
    value = os.environ.get("FAKE_CFG_" + a[2].replace(".", "_"))
    if value is None:
        print("Config path not set: " + a[2], file=sys.stderr)
        sys.exit(1)
    print(value)
elif a[:1] in (["gateway"], ["health"]) and os.environ.get("FAKE_GATEWAY_DOWN"):
    sys.exit(1)
else:
    print("{{}}")
"""

FAKE_FBM = """#!{python}
import os, sys
a = sys.argv[1:]
missing = os.environ.get("FAKE_FBM_MISSING", "").split(",")
if a[:2] == ["tool", "--list"]:
    print("context_pack(notes_chars, signals_top)")
elif a and a[0] in missing:
    print("No such command", file=sys.stderr)
    sys.exit(2)
elif a[:2] == ["automation", "list"]:
    print("No FBM scheduler entries installed.")
"""

EXISTING_JOBS = json.dumps(
    {"jobs": [{"id": "old1", "name": "fbm:morning-digest"}, {"id": "keep1", "name": "my-own-job"}]}
)
MUTATING = (("cron", "add"), ("cron", "remove"), ("config", "patch"), ("config", "unset"), ("message", "send"))


class Env:
    def __init__(self, tmp: Path):
        self.repo = tmp / "repo"
        for rel in ("agents", "openclaw"):
            shutil.copytree(ROOT / rel, self.repo / rel)
        (self.repo / "scripts").mkdir(parents=True)
        shutil.copy(ROOT / "scripts" / "render_openclaw_skill.py", self.repo / "scripts")
        shutil.copy(ROOT / "pyproject.toml", self.repo)
        venv_bin = self.repo / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        self.fbm = venv_bin / "fbm"
        self._script(venv_bin / "python", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        self._script(self.fbm, FAKE_FBM.format(python=sys.executable))
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self._script(self.bin / "openclaw", FAKE_OPENCLAW.format(python=sys.executable))
        self.home = tmp / "home"
        self.home.mkdir()
        self.log = tmp / "openclaw.log"
        self.settings = self.repo / "config" / "settings.toml"
        self.skill = self.home / ".openclaw" / "workspace" / "skills" / "fbm"
        self.state = self.repo / "data" / "openclaw" / "install-state"
        self.env = {
            "PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(self.home),
            "FAKE_LOG": str(self.log),
            "FAKE_CRON_LIST": EXISTING_JOBS,
            "TMPDIR": str(tmp),
        }

    @staticmethod
    def _script(path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        self.log.unlink(missing_ok=True)
        return subprocess.run(
            [BASH, str(self.repo / "openclaw" / "install.sh"), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=120,
        )

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def argvs(self, *prefix: str) -> list[list[str]]:
        return [c["argv"] for c in self.calls() if tuple(c["argv"][: len(prefix)]) == prefix]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _opt(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_install_registers_jobs_skill_and_alerts(env: Env) -> None:
    r = env.run("--chat-id", "123456", "--yes")
    assert r.returncode == 0, r.stderr
    adds = {_opt(a, "--name"): a for a in env.argvs("cron", "add")}
    assert set(adds) == {
        "fbm:data-sync",
        "fbm:health",
        "fbm:morning-digest",
        "fbm:weekly-preview",
        "fbm:lineup-guard",
        "fbm:injury-watch",
    }
    for name, argv in adds.items():
        assert _opt(argv, "--tz") == "America/New_York" and "--exact" in argv
        assert _opt(argv, "--command-cwd") == str(env.repo)
        command = json.loads(_opt(argv, "--command-argv"))
        assert command[0] == str(env.fbm)
        if name in ("fbm:data-sync", "fbm:health"):
            assert "--no-deliver" in argv and "--announce" not in argv
        else:
            assert "--announce" in argv and _opt(argv, "--channel") == "telegram" and _opt(argv, "--to") == "123456"
    assert json.loads(_opt(adds["fbm:morning-digest"], "--command-argv"))[1:] == ["digest", "--kind", "daily"]
    assert json.loads(_opt(adds["fbm:lineup-guard"], "--command-argv"))[1:] == ["alert", "lineup"]
    assert _opt(adds["fbm:data-sync"], "--timeout-seconds") == "1800"
    assert _opt(adds["fbm:lineup-guard"], "--cron") == "*/15 10-23 * * *"

    # The old same-named job is replaced (removed after the new one exists); other jobs are left alone.
    names = [c["argv"] for c in env.calls()]
    removes = env.argvs("cron", "remove")
    assert removes == [["cron", "remove", "old1"]]
    new_digest = next(i for i, a in enumerate(names) if a[:2] == ["cron", "add"] and "fbm:morning-digest" in a)
    assert names.index(["cron", "remove", "old1"]) > new_digest

    patch = next(c for c in env.calls() if c["argv"][:2] == ["config", "patch"])
    assert json.loads(patch["file"]) == {
        "cron": {"failureAlert": {"enabled": True, "mode": "announce", "channel": "telegram", "to": "123456"}}
    }
    assert env.argvs("message", "send") and _opt(env.argvs("message", "send")[0], "--target") == "123456"
    assert not env.argvs("approvals")  # exec mode unset = full: no allowlist change

    skill = (env.skill / "SKILL.md").read_text(encoding="utf-8")
    assert f"{env.fbm} tool <name>" in skill and "__FBM_BIN__" not in skill
    assert (env.skill / "references" / "matchup.md").is_file()
    assert not env.settings.exists()  # FBM's settings untouched without consent
    state = env.state.read_text(encoding="utf-8")
    assert "failure_alert=1" in state and "chat_id=123456" in state


def test_dry_run_changes_nothing(env: Env) -> None:
    r = env.run("--chat-id", "123456", "--yes", "--dry-run", "--notify-openclaw", "--allowlist")
    assert r.returncode == 0, r.stderr
    mutating = [a for a in (c["argv"] for c in env.calls()) if tuple(a[:2]) in MUTATING or a[:1] == ["approvals"]]
    assert mutating == []
    assert "[dry-run]" in r.stdout and "cron add --name fbm:lineup-guard" in r.stdout
    assert "render_openclaw_skill.py render" in r.stdout
    assert not env.skill.exists() and not env.state.exists() and not env.settings.exists()


def test_existing_failure_alert_is_kept(env: Env) -> None:
    r = env.run("--chat-id", "123456", "--yes", FAKE_CFG_cron_failureAlert='{"channel":"slack","to":"x"}')
    assert r.returncode == 0, r.stderr
    assert not env.argvs("config", "patch")
    assert "failure_alert=0" in env.state.read_text(encoding="utf-8")


def test_allowlist_added_when_exec_is_restricted(env: Env) -> None:
    r = env.run("--chat-id", "123456", "--yes", FAKE_CFG_tools_exec_mode="allowlist")
    assert r.returncode == 0, r.stderr
    assert env.argvs("approvals") == [["approvals", "allowlist", "add", "--agent", "main", str(env.fbm)]]
    r = env.run("--chat-id", "123456", "--no-allowlist", FAKE_CFG_tools_exec_mode="ask")
    assert r.returncode == 0 and not env.argvs("approvals")
    assert "openclaw approvals allowlist add" in r.stderr


def test_old_openclaw_skips_or_falls_back(env: Env) -> None:
    r = env.run("--chat-id", "123456", "--yes", FAKE_OC_VERSION="2026.5.30")
    assert r.returncode == 0, r.stderr
    assert not env.argvs("cron", "add") and "openclaw update" in r.stderr
    r = env.run("--chat-id", "123456", "--yes", "--fallback", FAKE_OC_VERSION="2026.5.30")
    adds = {_opt(a, "--name"): a for a in env.argvs("cron", "add")}
    assert set(adds) == {"fbm:morning-digest", "fbm:weekly-preview"}
    message = _opt(adds["fbm:morning-digest"], "--message")
    assert f"{env.fbm} digest --kind daily" in message and "NO_REPLY" in message
    assert "--command-argv" not in adds["fbm:morning-digest"]


def test_job_selection_and_schedule_overrides(env: Env) -> None:
    r = env.run(
        "--chat-id",
        "123456",
        "--yes",
        "--jobs",
        "lineup-guard,fbm:morning-digest",
        "--cron",
        "lineup-guard=*/10 18-23 * * *",
        FBM_CRON_MORNING_DIGEST="0 9 * * *",
    )
    assert r.returncode == 0, r.stderr
    adds = {_opt(a, "--name"): _opt(a, "--cron") for a in env.argvs("cron", "add")}
    assert adds == {"fbm:lineup-guard": "*/10 18-23 * * *", "fbm:morning-digest": "0 9 * * *"}


def test_jobs_skipped_when_fbm_lacks_the_command(env: Env) -> None:
    r = env.run("--chat-id", "123456", "--yes", FAKE_FBM_MISSING="digest,alert")
    assert r.returncode == 0, r.stderr
    assert {_opt(a, "--name") for a in env.argvs("cron", "add")} == {"fbm:data-sync", "fbm:health"}
    assert "no 'fbm digest' yet" in r.stderr


def test_notify_channel_only_with_consent_and_uninstall_reverts(env: Env) -> None:
    env.settings.parent.mkdir(parents=True)
    env.settings.write_text('[owner]\ntimezone = "America/Chicago"\n\n[notify]\nemail_enabled = true\n', "utf-8")
    r = env.run("--chat-id", "123456", "--yes", "--notify-openclaw")
    assert r.returncode == 0, r.stderr
    notify = tomllib.loads(env.settings.read_text(encoding="utf-8"))["notify"]
    assert notify["channels"] == ["openclaw", "email"] and notify["telegram_chat_id"] == "123456"
    assert notify["openclaw_bin"] == str(env.bin / "openclaw")
    assert (env.settings.parent / "settings.toml.bak").is_file()

    r = env.run("--uninstall", "--yes", FAKE_CFG_cron_failureAlert_to="123456")
    assert r.returncode == 0, r.stderr
    assert tomllib.loads(env.settings.read_text(encoding="utf-8"))["notify"]["channels"] == ["email"]
    assert env.argvs("cron", "remove") == [["cron", "remove", "old1"]]
    assert env.argvs("config", "unset") == [["config", "unset", "cron.failureAlert"]]
    assert env.argvs("approvals") == [["approvals", "allowlist", "remove", "--agent", "main", str(env.fbm)]]
    assert not env.skill.exists() and not env.state.exists()


def test_uninstall_leaves_changed_failure_alert_and_foreign_skill(env: Env) -> None:
    assert env.run("--chat-id", "123456", "--yes").returncode == 0
    r = env.run("--uninstall", FAKE_CFG_cron_failureAlert_to="999")
    assert r.returncode == 0, r.stderr
    assert not env.argvs("config", "unset")
    env.skill.mkdir(parents=True)
    (env.skill / "SKILL.md").write_text("---\nname: fbm\ndescription: mine\n---\n", encoding="utf-8")
    assert env.run("--uninstall").returncode == 0
    assert (env.skill / "SKILL.md").is_file()


def test_refuses_to_overwrite_a_foreign_skill(env: Env) -> None:
    env.skill.mkdir(parents=True)
    (env.skill / "SKILL.md").write_text("---\nname: fbm\ndescription: mine\n---\n", encoding="utf-8")
    r = env.run("--chat-id", "123456", "--yes")
    assert r.returncode == 1 and "not generated by FBM" in r.stderr
    assert not env.argvs("cron", "add")
    assert env.run("--chat-id", "123456", "--yes", "--force").returncode == 0
    assert "fbm-openclaw-skill" in (env.skill / "SKILL.md").read_text(encoding="utf-8")


def test_chat_id_from_settings_and_required(env: Env) -> None:
    r = env.run("--yes")
    assert r.returncode == 1 and "chat id" in r.stderr
    env.settings.parent.mkdir(parents=True)
    env.settings.write_text("[notify]\ntelegram_chat_id = 987654321\n", encoding="utf-8")
    r = env.run("--yes", "--no-jobs")
    assert r.returncode == 0, r.stderr
    assert _opt(env.argvs("message", "send")[0], "--target") == "987654321"
    assert not env.argvs("cron")


def test_gateway_down_stops_before_changing_anything(env: Env) -> None:
    r = env.run("--chat-id", "123456", "--yes", FAKE_GATEWAY_DOWN="1")
    assert r.returncode == 1 and "Gateway isn't reachable" in r.stderr
    assert not env.skill.exists()
