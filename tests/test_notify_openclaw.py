"""OpenClaw delivery: argv (never a shell), --silent, failures that let the dispatcher fall back, binary lookup."""

import os
import subprocess
from pathlib import Path

import pytest

from fbm.notify import openclaw
from fbm.notify.markdown import split_markdown
from fbm.settings import Settings

SETTINGS = Settings(telegram_chat_id="4242", openclaw_bin="openclaw")


class FakeRun:
    def __init__(self, code=0, out="", err=""):
        self.code, self.out, self.err, self.calls = code, out, err, []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, self.code, self.out, self.err)


def test_send_runs_message_send_without_a_shell(tmp_path):
    binary = tmp_path / "oc" / "openclaw"  # absolute on every OS ("/opt/oc/..." has no drive, so not on Windows)
    run = FakeRun()
    n = openclaw.send("**Hi**", silent=True, settings=SETTINGS, runner=run, resolver=lambda b: [str(binary)])
    cmd, kw = run.calls[0]
    assert n == 1
    assert cmd == [
        str(binary),
        "message",
        "send",
        "--channel",
        "telegram",
        "--target",
        "4242",
        "--message",
        "**Hi**",
        "--silent",
    ]
    assert kw["timeout"] == openclaw.TIMEOUT_S and "shell" not in kw and kw["stdin"] is subprocess.DEVNULL
    assert kw["env"]["PATH"].split(os.pathsep)[0] == str(binary.parent)  # npm installs need node next to the binary
    openclaw.send("x", settings=SETTINGS, runner=run, resolver=lambda b: ["oc"])
    assert "--silent" not in run.calls[-1][0]


def test_failures_raise_with_a_short_excerpt_and_no_message_body():
    secret_text = "Lineup alert: bench Smith " * 3
    run = FakeRun(code=2, err=f"error: delivery failed for {secret_text} (gateway offline)")
    with pytest.raises(openclaw.OpenClawError, match="exited 2") as err:
        openclaw.send(secret_text, settings=SETTINGS, runner=run, resolver=lambda b: ["oc"])
    assert "bench Smith" not in str(err.value) and "gateway offline" in str(err.value)

    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 60)

    with pytest.raises(openclaw.OpenClawError, match="timed out"):
        openclaw.send("x", settings=SETTINGS, runner=timeout, resolver=lambda b: ["oc"])

    def missing(cmd, **kw):
        raise FileNotFoundError(2, "No such file or directory")

    with pytest.raises(openclaw.OpenClawError, match="could not start"):
        openclaw.send("x", settings=SETTINGS, runner=missing, resolver=lambda b: ["oc"])
    with pytest.raises(openclaw.OpenClawError, match="chat id"):
        openclaw.send("x", settings=Settings(), runner=run, resolver=lambda b: ["oc"])


def test_long_messages_go_out_in_parts():
    run = FakeRun()
    body = "\n\n".join("p" * 2000 for _ in range(3))
    assert openclaw.send(body, settings=SETTINGS, runner=run, resolver=lambda b: ["oc"]) == 3
    assert all(len(c[0][8]) <= openclaw.MAX_CHARS for c in run.calls)


def test_locate_tries_path_then_common_install_folders(tmp_path):
    assert openclaw.locate("openclaw", which=lambda n: "/usr/bin/openclaw", home=tmp_path) == Path("/usr/bin/openclaw")
    nvm = tmp_path / ".nvm" / "versions" / "node" / "v22.1.0" / "bin"
    nvm.mkdir(parents=True)
    (nvm / "openclaw").write_text("#!/usr/bin/env node\n")
    assert openclaw.locate("openclaw", which=lambda n: None, home=tmp_path) == nvm / "openclaw"
    explicit = tmp_path / "bin" / "oc"
    assert openclaw.locate(str(explicit), which=lambda n: None, home=tmp_path) is None
    explicit.parent.mkdir()
    explicit.write_text("")
    assert openclaw.locate(str(explicit), which=lambda n: None, home=tmp_path) == explicit
    with pytest.raises(openclaw.OpenClawError, match="not found"):
        openclaw.resolve("nope-claw", which=lambda n: None, home=tmp_path / "empty")


def test_windows_cmd_shim_runs_the_node_script_directly(tmp_path):
    shim = tmp_path / "openclaw.cmd"
    script = tmp_path / "node_modules" / "openclaw" / "openclaw.mjs"
    script.parent.mkdir(parents=True)
    script.write_text("")
    shim.write_text('@ECHO off\r\n"%_prog%"  "%dp0%\\node_modules\\openclaw\\openclaw.mjs" %*\r\n')
    assert openclaw.shim_command(shim, which=lambda n: "/usr/bin/node") == ["/usr/bin/node", str(script)]
    assert openclaw.resolve(str(shim), which=lambda n: "/usr/bin/node") == ["/usr/bin/node", str(script)]
    shim.write_text("@ECHO off\r\nnode something %*\r\n")
    with pytest.raises(openclaw.OpenClawError, match="cannot run safely"):
        openclaw.shim_command(shim, which=lambda n: "/usr/bin/node")


def test_split_markdown_keeps_code_fences_well_formed():
    code = "```\n" + "\n".join(f"row {i}" for i in range(200)) + "\n```"
    parts = split_markdown("intro\n\n" + code + "\n\noutro", 300)
    assert all(len(p) <= 300 for p in parts)
    for p in parts:
        assert p.count("```") % 2 == 0
    assert parts[0].startswith("intro") and parts[-1].endswith("outro")
    assert split_markdown("short", 300) == ["short"]
    assert split_markdown("", 300) == []
