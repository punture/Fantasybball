"""docs/commands.md is linked as "every command": each visible CLI command must have a row there."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import typer.main

from fbm.cli import app

COMMANDS_DOC = Path(__file__).resolve().parents[1] / "docs" / "commands.md"


def _leaf_commands(group: Any, prefix: tuple[str, ...] = ()) -> Iterator[str]:
    """`fbm <group> <command>` for every visible command (groups have `commands`; recent Typer vendors click)."""
    for name, sub in group.commands.items():
        if getattr(sub, "hidden", False):
            continue
        if hasattr(sub, "commands"):
            yield from _leaf_commands(sub, (*prefix, name))
        else:
            yield " ".join(("fbm", *prefix, name))


def test_every_cli_command_is_documented():
    doc = COMMANDS_DOC.read_text(encoding="utf-8")
    commands = sorted(_leaf_commands(typer.main.get_command(app)))
    assert "fbm flaim login" in commands and "fbm protect" in commands
    missing = [c for c in commands if f"`{c}`" not in doc and f"`{c} " not in doc]
    assert missing == [], f"add a row to docs/commands.md for: {', '.join(missing)}"
