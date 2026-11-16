"""Deliver through the owner's OpenClaw assistant: `openclaw message send --channel telegram --target <chat id>`.

The CLI runs without a shell (argv list, 60 s timeout). Scheduled runs often have a minimal PATH, so the binary is
also looked up in common install folders, and its own folder is put first on PATH (npm installs start with
`#!/usr/bin/env node`). On Windows an npm `.cmd` shim is never run through cmd.exe (message text could inject
commands); FBM runs the shim's Node script directly. Any failure raises OpenClawError so the dispatcher can fall
back to direct Telegram. Failures never echo the message beyond a short excerpt of OpenClaw's own output.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from fbm.notify.base import NotifyError
from fbm.notify.markdown import split_markdown
from fbm.settings import Settings, load_settings

TIMEOUT_S = 60
MAX_CHARS = 3500  # longer messages go out in several parts, split between paragraphs
_SHIM_SCRIPT = re.compile(r'"%~?dp0%?\\([^"%]+\.[cm]?js)"', re.IGNORECASE)

Which = Callable[[str], str | None]
Runner = Callable[..., subprocess.CompletedProcess]


class OpenClawError(NotifyError):
    pass


def _common_dirs(home: Path) -> list[Path]:
    dirs = [
        home / ".npm-global" / "bin",
        home / ".local" / "bin",
        home / ".openclaw" / "bin",
        home / ".bun" / "bin",
        home / ".volta" / "bin",
        home / ".local" / "share" / "pnpm",
        home / "Library" / "pnpm",
        *sorted((home / ".nvm" / "versions" / "node").glob("*/bin"), reverse=True),
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path("/usr/bin"),
    ]
    if appdata := os.environ.get("APPDATA"):
        dirs.append(Path(appdata) / "npm")
    return dirs


def locate(binary: str, which: Which = shutil.which, home: Path | None = None) -> Path | None:
    """Path of the OpenClaw CLI: an explicit path, else PATH, else common install folders."""
    path = Path(binary).expanduser()
    if path.is_absolute() or len(path.parts) > 1:
        return path if path.is_file() else None
    if found := which(binary):
        return Path(found)
    names = [binary, f"{binary}.cmd", f"{binary}.exe"] if os.name == "nt" else [binary]
    for d in _common_dirs(home or Path.home()):
        for name in names:
            if (d / name).is_file():
                return d / name
    return None


def shim_command(shim: Path, which: Which = shutil.which) -> list[str]:
    """[node, script] behind an npm Windows `.cmd` shim, so no argument ever passes through cmd.exe."""
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError as err:
        raise OpenClawError(f"Cannot read {shim.name}: {err.strerror or type(err).__name__}") from None
    match = _SHIM_SCRIPT.search(text)
    if not match:
        raise OpenClawError(
            f"{shim.name} is a cmd.exe shim FBM cannot run safely: set [notify] openclaw_bin to OpenClaw's .exe or "
            "Node script."
        )
    script = shim.parent / match.group(1).replace("\\", os.sep)
    local_node = shim.parent / "node.exe"
    node = str(local_node) if local_node.is_file() else which("node")
    if not node or not script.is_file():
        raise OpenClawError(f"{shim.name}: Node.js or OpenClaw's script ({script.name}) not found.")
    return [node, str(script)]


def resolve(binary: str = "openclaw", which: Which = shutil.which, home: Path | None = None) -> list[str]:
    """argv prefix that runs the OpenClaw CLI without a shell; raises OpenClawError when it isn't installed."""
    path = locate(binary, which, home)
    if path is None:
        raise OpenClawError(
            f"OpenClaw CLI '{binary}' not found (PATH and common install folders): set [notify] openclaw_bin to its "
            "full path, or remove 'openclaw' from [notify] channels."
        )
    if path.suffix.lower() in (".cmd", ".bat"):
        return shim_command(path, which)
    return [str(path)]


def _env(argv: Sequence[str]) -> dict[str, str]:
    env = dict(os.environ)
    folders = dict.fromkeys(str(Path(a).parent) for a in argv[:1] if Path(a).is_absolute())
    env["PATH"] = os.pathsep.join([*folders, env.get("PATH", "")])
    return env


def excerpt(output: str, message: str, chars: int = 200) -> str:
    """The tail of OpenClaw's output, without the message text."""
    text = (output or "").replace(message, "<message>") if message else (output or "")
    text = " ".join(text.split())
    return text[-chars:] or "no output"


def send(
    text: str,
    *,
    silent: bool = False,
    settings: Settings | None = None,
    runner: Runner = subprocess.run,
    resolver: Callable[[str], list[str]] = resolve,
) -> int:
    """Send Markdown `text` to the owner's Telegram chat through OpenClaw. Returns the number of messages sent."""
    settings = settings or load_settings()
    chat_id = settings.telegram_chat_id.strip()
    if not chat_id:
        raise OpenClawError("No Telegram chat id: run `fbm notify setup` ([notify] telegram_chat_id).")
    parts = split_markdown(text, MAX_CHARS)
    if not parts:
        raise OpenClawError("Nothing to send (empty message).")
    argv = resolver(settings.openclaw_bin)
    for i, part in enumerate(parts):
        cmd = [*argv, "message", "send", "--channel", "telegram", "--target", chat_id, "--message", part]
        if silent:
            cmd.append("--silent")
        after = f" (after {i} of {len(parts)} parts were sent)" if i else ""
        try:
            p = runner(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=TIMEOUT_S,
                env=_env(argv),
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise OpenClawError(f"OpenClaw timed out after {TIMEOUT_S}s{after}.") from None
        except OSError as err:
            raise OpenClawError(f"OpenClaw could not start ({err.strerror or type(err).__name__}){after}.") from None
        if p.returncode != 0:
            raise OpenClawError(f"OpenClaw exited {p.returncode}: {excerpt(p.stderr or p.stdout, part)}{after}")
    return len(parts)
