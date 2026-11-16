"""Splitting FBM's Markdown messages into parts that fit a chat message (raw Markdown, for OpenClaw)."""

from __future__ import annotations

import re

_FENCE = re.compile(r"^\s*```")


def paragraphs(markdown: str) -> list[str]:
    """Blank-line separated paragraphs; a ``` block stays whole even when it contains blank lines."""
    out: list[str] = []
    cur: list[str] = []
    fenced = False
    for line in markdown.replace("\r\n", "\n").split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
        if not line.strip() and not fenced:
            if cur:
                out.append("\n".join(cur))
            cur = []
        else:
            cur.append(line)
    if cur:
        out.append("\n".join(cur))
    return out


def _hard_split(line: str, limit: int) -> list[str]:
    pieces = []
    while len(line) > limit:
        space = line.rfind(" ", 1, limit + 1)
        cut = space if space > limit // 2 else limit
        pieces.append(line[:cut])
        line = line[cut:].lstrip(" ")
    return [*pieces, line] if line else pieces


def _lines(lines: list[str], limit: int) -> list[str]:
    return [p for ln in lines for p in (_hard_split(ln, limit) if len(ln) > limit else [ln])]


def _split_paragraph(para: str, limit: int) -> list[str]:
    lines = para.split("\n")
    if not _FENCE.match(lines[0]):
        return _lines(lines, limit)
    opener = lines[0].strip()
    body = lines[1:-1] if len(lines) > 1 and _FENCE.match(lines[-1]) else lines[1:]
    budget = max(1, limit - len(opener) - len("\n\n```"))
    groups: list[list[str]] = [[]]
    for ln in _lines(body, budget):
        if groups[-1] and len("\n".join([*groups[-1], ln])) > budget:
            groups.append([])
        groups[-1].append(ln)
    return [f"{opener}\n" + "\n".join(g) + "\n```" for g in groups]


def split_markdown(markdown: str, limit: int) -> list[str]:
    """Parts of at most `limit` characters, split between paragraphs, then lines; a code block that has to be
    split is closed and reopened so every part stays well-formed."""
    units: list[tuple[str, str]] = []  # (text, separator before it)
    for i, para in enumerate(paragraphs(markdown)):
        pieces = [para] if len(para) <= limit else _split_paragraph(para, limit)
        units += [(p, ("\n\n" if i else "") if j == 0 else "\n") for j, p in enumerate(pieces)]
    out: list[str] = []
    cur = ""
    for text, sep in units:
        if cur and len(cur) + len(sep) + len(text) > limit:
            out.append(cur)
            cur = text
        else:
            cur = cur + sep + text if cur else text
    if cur:
        out.append(cur)
    return out
