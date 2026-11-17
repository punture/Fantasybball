"""openclaw/install.ps1 end to end on Windows, against fake `openclaw.cmd` and `fbm.cmd` (no network, no real OpenClaw).

The fakes are cmd.exe shims like npm's `openclaw.cmd`, so every argument takes the real route: Windows PowerShell 5.1,
then cmd.exe, then the program's own argument parsing. The FBM checkout sits in a folder with a space, as Windows user
folders often do. Each test works in a temp folder: nothing touches the real checkout, OpenClaw or settings.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell")
pytestmark = pytest.mark.skipif(sys.platform != "win32" or POWERSHELL is None, reason="Windows PowerShell installer")

FAKE_OPENCLAW = """\
import json, os, sys
a = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(a) + "\\n")
if a == ["--version"]:
    print("OpenClaw 2026.10.9 (abc1234)")
elif a[:2] == ["cron", "list"]:
    print('{"jobs": []}')
elif a[:2] == ["config", "get"]:
    value = os.environ.get("FAKE_CFG_" + a[2].replace(".", "_"))
    if value is None:
        print("Config path not set: " + a[2], file=sys.stderr)
        sys.exit(1)
    print(value)
elif a[:2] == ["skills", "info"]:
    sys.exit(1)  # not listed yet
else:
    print("{}")
"""

FAKE_FBM = """\
import sys
a = sys.argv[1:]
if a[:2] == ["tool", "--list"]:
    print("context_pack(notes_chars, signals_top)")
elif a[:2] == ["automation", "list"]:
    print("No FBM scheduler entries installed.")
"""


def _shim(path: Path, script: Path) -> None:
    """A cmd.exe shim that hands its arguments on with %*, as npm's openclaw.cmd does."""
    path.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")


class Env:
    def __init__(self, tmp: Path):
        self.repo = tmp / "fbm checkout"
        for rel in ("agents", "openclaw"):
            shutil.copytree(ROOT / rel, self.repo / rel)
        (self.repo / "scripts").mkdir(parents=True)
        shutil.copy(ROOT / "scripts" / "render_openclaw_skill.py", self.repo / "scripts")
        shutil.copy(ROOT / "pyproject.toml", self.repo)
        fakes = tmp / "fakes"
        fakes.mkdir()
        (fakes / "openclaw.py").write_text(FAKE_OPENCLAW, encoding="utf-8")
        (fakes / "fbm.py").write_text(FAKE_FBM, encoding="utf-8")
        self.fbm = self.repo / ".venv" / "Scripts" / "fbm.cmd"
        self.fbm.parent.mkdir(parents=True)
        _shim(self.fbm, fakes / "fbm.py")
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        _shim(bin_dir / "openclaw.cmd", fakes / "openclaw.py")
        self.log = tmp / "openclaw.log"
        system = os.environ.get("SystemRoot", r"C:\Windows")
        path = [bin_dir, Path(sys.executable).parent, Path(system, "System32"), Path(system)]
        self.env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("FBM_", "OPENCLAW_"))}
        self.env.update(
            PATH=os.pathsep.join(map(str, path)),
            FAKE_LOG=str(self.log),
            OPENCLAW_WORKSPACE_DIR=str(tmp / "workspace"),
        )

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        self.log.unlink(missing_ok=True)
        script = self.repo / "openclaw" / "install.ps1"
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)]
            + ["-FbmBin", str(self.fbm), "-ChatId", "123456", "-Yes", *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=300,
        )

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _opt(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_jobs_run_fbm_directly_from_a_folder_with_a_space(env: Env) -> None:
    r = env.run()
    assert r.returncode == 0, r.stdout + r.stderr
    adds = {_opt(a, "--name"): a for a in env.calls() if a[:2] == ["cron", "add"]}
    assert len(adds) == 8
    for argv in adds.values():
        assert "--command" not in argv  # OpenClaw runs --command through `sh -lc`, which drops a path's backslashes
        assert Path(_opt(argv, "--command-cwd")) == env.repo
        assert Path(json.loads(_opt(argv, "--command-argv"))[0]) == env.fbm
    assert json.loads(_opt(adds["fbm:morning-digest"], "--command-argv"))[1:] == ["digest", "--kind", "daily"]
    assert json.loads(_opt(adds["fbm:injury-watch"], "--command-argv"))[1:] == ["alert", "injuries"]
    assert json.loads(_opt(adds["fbm:health"], "--command-argv"))[1:] == ["health"]


def test_skill_check_names_the_agent_and_flags_a_skill_allowlist(env: Env) -> None:
    r = env.run("-NoJobs", FAKE_CFG_agents_defaults_skills='["weather"]', FAKE_CFG_skills_load_watch="false")
    assert r.returncode == 0, r.stdout + r.stderr
    assert ["skills", "info", "fbm", "--agent", "main"] in env.calls()
    assert "skills.load.watch is off" in r.stdout
    assert "only uses the skills listed in agents.defaults.skills" in r.stdout
    r = env.run("-NoJobs", FAKE_CFG_agents_entries_main_skills='["weather", "fbm"]')
    assert r.returncode == 0, r.stdout + r.stderr
    assert "only uses the skills" not in r.stdout
