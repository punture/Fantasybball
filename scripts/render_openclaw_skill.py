"""Render FBM's OpenClaw skill, plus the small pure helpers openclaw/install.sh and install.ps1 share. Stdlib only.

Subcommands (run with the FBM checkout's own Python, e.g. `.venv/bin/python scripts/render_openclaw_skill.py ...`):

  render --bin PATH --out DIR [--repo DIR] [--platform auto|posix|windows] [--force]
      Write DIR/SKILL.md (from openclaw/skills/fbm/SKILL.md.tmpl, `__FBM_BIN__` -> the FBM command) and
      DIR/references/*.md (agents/skills/*/SKILL.md and agents/roles/*.md, `uv run fbm` -> the FBM command).
      Refuses (exit 3) to overwrite a SKILL.md FBM did not generate unless --force.
  chat-id [--value ID] [--settings FILE]
      Print the Telegram chat id (from --value, else [notify] telegram_chat_id); exit 1 if missing or invalid.
  version TEXT [--min X.Y.Z]
      Print the version found in TEXT (e.g. `openclaw --version` output); exit 0 if >= --min, 1 if older,
      2 if no version is found.
  job-ids [--prefix P] [--file FILE]
      Read `openclaw cron list --json` output (FILE or stdin); print "<id>\\t<name>" for jobs whose name starts
      with P (default "fbm:").
  notify-channel --settings FILE (--add NAME | --remove NAME) [--chat-id ID] [--openclaw-bin PATH]
      Add/remove NAME in config/settings.toml [notify] channels (backup to settings.toml.bak first). --add also sets
      telegram_chat_id / openclaw_bin when they are missing. Never touches other keys.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = Path("openclaw") / "skills" / "fbm" / "SKILL.md.tmpl"
BIN_TOKEN = "__FBM_BIN__"
MARKER = "fbm-openclaw-skill"  # in every generated SKILL.md: installers only replace or remove what FBM generated
SKILL_NAME = "fbm"
JOB_PREFIX = "fbm:"
MAX_DESCRIPTION = 160
UV_RUN_FBM = re.compile(r"\buv run fbm\b")
VERSION = re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})")
CHAT_ID = re.compile(r"^(-?\d{3,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$")
WINDOWS_PATH = re.compile(r"^([A-Za-z]:[\\/]|\\\\)")
WINDOWS_BARE_OK = re.compile(r"^[A-Za-z0-9_.:\\/-]+$")


class SkillConflictError(RuntimeError):
    """The target folder holds a SKILL.md that FBM did not generate."""


# ------------------------------------------------------------------ rendering


def is_windows_path(path: str) -> bool:
    return bool(WINDOWS_PATH.match(path))


def command_prefix(bin_path: str, platform: str = "auto") -> str:
    """How the agent's exec tool should spell the FBM program: POSIX sh quoting, or PowerShell on Windows."""
    windows = platform == "windows" or (platform == "auto" and is_windows_path(bin_path))
    if not windows:
        return shlex.quote(bin_path)
    if WINDOWS_BARE_OK.match(bin_path):
        return bin_path
    return "& '" + bin_path.replace("'", "''") + "'"  # PowerShell call operator + literal string


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """`---` YAML-ish frontmatter (one `key: value` per line) -> (fields, body)."""
    m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n?", text, flags=re.S)
    if not m:
        return {}, text
    fields = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            fields[key.strip()] = value.strip().strip("\"'")
    return fields, text[m.end() :]


def _with_prefix(text: str, prefix: str) -> str:
    # A function replacement: Windows paths contain backslashes that re.sub would read as escapes.
    return UV_RUN_FBM.sub(lambda _m: prefix, text)


def render_skill(template: str, prefix: str) -> str:
    return template.replace(BIN_TOKEN, prefix)


def render_reference(source: str, text: str, prefix: str) -> str:
    meta, body = split_frontmatter(text)
    lines = [
        f"<!-- {MARKER}: rendered from {source} by FBM's OpenClaw installer; re-run it to update. -->",
        "",
        f"> Playbook for the `fbm` skill. Run every FBM tool named here as `{prefix} tool <name> key=value` "
        "(exec, one command per call) and follow the fbm skill's rules.",
    ]
    if meta.get("description"):
        lines.append(f"> When: {meta['description']}")
    tools = meta.get("tools", "").strip("[]")
    if tools:
        lines.append(f"> Tools this role may use: {', '.join(t.strip() for t in tools.split(',') if t.strip())}.")
    return "\n".join(lines) + "\n\n" + _with_prefix(body.lstrip("\n"), prefix).rstrip("\n") + "\n"


def plan(repo: Path, bin_path: str, platform: str = "auto") -> dict[str, str]:
    """Every generated file (relative path -> content), in a deterministic order."""
    prefix = command_prefix(bin_path, platform)
    files = {"SKILL.md": render_skill((repo / TEMPLATE).read_text(encoding="utf-8"), prefix)}
    for path in sorted((repo / "agents" / "skills").glob("*/SKILL.md")):
        source = f"agents/skills/{path.parent.name}/SKILL.md"
        files[f"references/{path.parent.name}.md"] = render_reference(source, path.read_text(encoding="utf-8"), prefix)
    for path in sorted((repo / "agents" / "roles").glob("*.md")):
        source = f"agents/roles/{path.name}"
        files[f"references/role-{path.stem}.md"] = render_reference(source, path.read_text(encoding="utf-8"), prefix)
    return files


def is_generated(skill_dir: Path) -> bool:
    skill = skill_dir / "SKILL.md"
    return skill.is_file() and MARKER in skill.read_text(encoding="utf-8", errors="replace")


def write_skill(out: Path, files: dict[str, str], force: bool = False) -> list[str]:
    if (out / "SKILL.md").exists() and not is_generated(out) and not force:
        raise SkillConflictError(f"{out / 'SKILL.md'} was not generated by FBM; refusing to overwrite (use --force).")
    if (out / "references").is_dir():
        shutil.rmtree(out / "references")  # drop playbooks that no longer exist upstream
    for rel, content in files.items():
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))  # LF on every OS: deterministic output
    return list(files)


# ------------------------------------------------------------------ small helpers


def frontmatter_problems(text: str) -> list[str]:
    """OpenClaw skill frontmatter rules: name = lowercase-hyphen slug, description = one line under 160 chars."""
    meta, _ = split_frontmatter(text)
    problems = []
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", meta.get("name", "")):
        problems.append("name must be lowercase letters, digits and hyphens")
    description = meta.get("description", "")
    if not description or len(description) >= MAX_DESCRIPTION:
        problems.append(f"description must be one line under {MAX_DESCRIPTION} characters")
    return problems


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = VERSION.search(text)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def valid_chat_id(value: str) -> bool:
    return bool(CHAT_ID.match(value.strip()))


def read_settings(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return {}


def settings_chat_id(path: Path) -> str:
    value = read_settings(path).get("notify", {}).get("telegram_chat_id", "")
    return str(value).strip() if value is not None else ""


def _job_rows(data: object) -> list[dict]:
    if isinstance(data, dict):
        for key in ("jobs", "data", "items", "results", "automations"):
            if isinstance(data.get(key), list):
                return [j for j in data[key] if isinstance(j, dict)]
        return []
    return [j for j in data if isinstance(j, dict)] if isinstance(data, list) else []


def _first_json(text: str) -> object:
    """The first JSON object/array in text, skipping any banner lines printed before it."""
    error = None
    for m in re.finditer(r"[\[{]", text):
        try:
            return json.JSONDecoder().raw_decode(text, m.start())[0]
        except json.JSONDecodeError as err:
            error = error or err
    if error:
        raise error
    return []


def job_ids(text: str, prefix: str = JOB_PREFIX) -> list[tuple[str, str]]:
    """(id, name) of every job in `openclaw cron list --json` output whose name starts with prefix."""
    out = []
    for job in _job_rows(_first_json(text)):
        name, job_id = str(job.get("name") or ""), str(job.get("id") or job.get("jobId") or "")
        if job_id and name.startswith(prefix):
            out.append((job_id, name))
    return out


def _toml_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)  # a JSON string/array of strings is valid TOML


def effective_channels(notify: dict) -> list[str]:
    if "channels" in notify:
        return [str(c) for c in notify["channels"]]
    return ["email"] if notify.get("email_enabled") else []


def update_notify(text: str, set_keys: dict[str, object]) -> str:
    """Set keys in the [notify] table of a settings.toml text, keeping every other line as it is."""
    lines = text.splitlines()
    header = next((i for i, line in enumerate(lines) if re.match(r"\s*\[notify\]\s*(#.*)?$", line)), None)
    if header is None:
        block = ["", "[notify]", *(f"{k} = {_toml_value(v)}" for k, v in set_keys.items())]
        return "\n".join([*lines, *block]).lstrip("\n") + "\n"
    end = next((i for i in range(header + 1, len(lines)) if re.match(r"\s*\[", lines[i])), len(lines))
    for key, value in set_keys.items():
        new_line = f"{key} = {_toml_value(value)}"
        start = next((i for i in range(header + 1, end) if re.match(rf"\s*{re.escape(key)}\s*=", lines[i])), None)
        if start is None:
            lines.insert(header + 1, new_line)
            end += 1
            continue
        stop, depth = start, lines[start].count("[") - lines[start].count("]")
        while depth > 0 and stop + 1 < end:  # a multi-line array
            stop += 1
            depth += lines[stop].count("[") - lines[stop].count("]")
        lines[start : stop + 1] = [new_line]
        end -= stop - start
    return "\n".join(lines) + "\n"


def change_notify_channel(
    path: Path, add: str | None = None, remove: str | None = None, chat_id: str = "", openclaw_bin: str = ""
) -> list[str]:
    """Add or remove one notify channel in settings.toml; returns the resulting channel list."""
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    notify = tomllib.loads(text).get("notify", {})
    channels = effective_channels(notify)
    set_keys: dict[str, object] = {}
    if add and add not in channels:
        set_keys["channels"] = [add, *channels]
    if remove and remove in channels:
        set_keys["channels"] = [c for c in channels if c != remove]
    if add and chat_id and not str(notify.get("telegram_chat_id", "")).strip():
        set_keys["telegram_chat_id"] = chat_id
    if add and openclaw_bin and not str(notify.get("openclaw_bin", "")).strip():
        set_keys["openclaw_bin"] = openclaw_bin
    if not set_keys:
        return channels
    new_text = update_notify(text, set_keys)
    result = tomllib.loads(new_text).get("notify", {})  # never write a file that no longer parses
    if path.is_file():
        shutil.copyfile(path, path.with_name(path.name + ".bak"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(new_text.encode("utf-8"))
    return effective_channels(result)


# ------------------------------------------------------------------ CLI


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Render FBM's OpenClaw skill and installer helpers.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="Render the fbm skill into a folder.")
    r.add_argument("--bin", required=True, help="Absolute path of the FBM executable.")
    r.add_argument("--out", required=True, type=Path, help="Skill folder, e.g. ~/.openclaw/workspace/skills/fbm.")
    r.add_argument("--repo", type=Path, default=REPO, help="FBM checkout (default: this script's repo).")
    r.add_argument("--platform", choices=("auto", "posix", "windows"), default="auto")
    r.add_argument("--force", action="store_true", help="Overwrite a SKILL.md FBM did not generate.")
    c = sub.add_parser("chat-id", help="Print the Telegram chat id to use.")
    c.add_argument("--value", default="")
    c.add_argument("--settings", type=Path, default=REPO / "config" / "settings.toml")
    v = sub.add_parser("version", help="Compare a version string against a minimum.")
    v.add_argument("text")
    v.add_argument("--min", default="0.0.0")
    j = sub.add_parser("job-ids", help="List FBM job ids from `openclaw cron list --json` output.")
    j.add_argument("--prefix", default=JOB_PREFIX)
    j.add_argument("--file", type=Path)
    n = sub.add_parser("notify-channel", help="Add/remove a channel in [notify] channels.")
    n.add_argument("--settings", type=Path, required=True)
    group = n.add_mutually_exclusive_group(required=True)
    group.add_argument("--add")
    group.add_argument("--remove")
    n.add_argument("--chat-id", default="")
    n.add_argument("--openclaw-bin", default="")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "render":
        try:
            written = write_skill(args.out, plan(args.repo, args.bin, args.platform), args.force)
        except SkillConflictError as err:
            print(err, file=sys.stderr)
            return 3
        print(f"Rendered {len(written)} files into {args.out}")
        return 0
    if args.cmd == "chat-id":
        value = (args.value or settings_chat_id(args.settings)).strip()
        if not valid_chat_id(value):
            print(
                "No valid Telegram chat id (a number like 123456789, -100... for groups, or @channel).", file=sys.stderr
            )
            return 1
        print(value)
        return 0
    if args.cmd == "version":
        found, minimum = parse_version(args.text), parse_version(args.min) or (0, 0, 0)
        if found is None:
            return 2
        print(".".join(map(str, found)))
        return 0 if found >= minimum else 1
    if args.cmd == "job-ids":
        text = args.file.read_text(encoding="utf-8-sig") if args.file else sys.stdin.read()
        try:
            rows = job_ids(text, args.prefix)
        except json.JSONDecodeError:
            print("Could not read the job list (not JSON).", file=sys.stderr)
            return 2
        for job_id, name in rows:
            print(f"{job_id}\t{name}")
        return 0
    channels = change_notify_channel(args.settings, args.add, args.remove, args.chat_id, args.openclaw_bin)
    print("[notify] channels = " + json.dumps(channels))
    return 0


if __name__ == "__main__":
    sys.exit(main())
