"""Telegram alerts through the Bot API (sendMessage only).

FBM writes messages in a small Markdown subset (**bold**, *italic*, `code`, ``` blocks, "- " bullets, # headings,
[links](https://...), | tables |). Telegram gets them as HTML: everything else is escaped, a message is at most
MAX_CHARS characters, and messages split on paragraph/line boundaries, never inside a <pre> block.

FBM never calls getUpdates on its own: OpenClaw may be polling the same bot. `detect_chat_ids` (one explicit
getUpdates call) exists only for `fbm notify setup --detect-chat-id`. The bot token never appears in errors or logs.
"""

from __future__ import annotations

import html
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from fbm import credentials
from fbm.notify.base import NotifyError
from fbm.settings import Settings, load_settings

API = "https://api.telegram.org"
MAX_CHARS = 4000  # Telegram allows 4096 after entity parsing; HTML length is a safe upper bound
ATTEMPTS = 3
MAX_RETRY_AFTER_S = 60  # longest 429 back-off FBM waits out
TIMEOUT_S = 20.0
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")

Sleep = Callable[[float], None]


class TelegramError(NotifyError):
    pass


# ---------------------------------------------------------------- Markdown subset -> Telegram HTML (pure)

_FENCE = re.compile(r"^\s*```")
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_BULLET = re.compile(r"^[-*+]\s+(.*)$")
_RULE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
_TABLE_RULE = re.compile(r"^\|?[\s:|-]+\|?$")
_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])")
_TAG = re.compile(r"<[^>]+>")


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _styled(text: str) -> str:
    out = _escape(text)
    out = _LINK.sub(lambda m: f'<a href="{m.group(2).replace(chr(34), "&quot;")}">{m.group(1)}</a>', out)
    out = _BOLD.sub(r"<b>\1</b>", out)
    return _ITALIC.sub(r"<i>\1</i>", out)


def inline(text: str) -> str:
    """One line of Markdown -> Telegram HTML (code spans are escaped verbatim)."""
    parts, pos = [], 0
    for m in _CODE.finditer(text):
        parts += [_styled(text[pos : m.start()]), f"<code>{_escape(m.group(1))}</code>"]
        pos = m.end()
    parts.append(_styled(text[pos:]))
    return "".join(parts)


def _line(line: str) -> str:
    body = line.strip()
    indent = " " * (len(line) - len(line.lstrip()))
    if m := _HEADING.match(body):
        return f"<b>{inline(m.group(1))}</b>"
    if m := _BULLET.match(body):
        return f"{indent}• {inline(m.group(1))}"
    if _RULE.match(body):
        return "──────────"
    return indent + inline(body)


def _table(rows: list[str]) -> list[str]:
    """Markdown table rows -> aligned plain-text rows (shown in a <pre> block)."""
    cells = [
        [c.strip().replace("**", "").replace("`", "") for c in r.strip().strip("|").split("|")]
        for r in rows
        if not _TABLE_RULE.match(r.strip())
    ]
    if not cells:
        return []
    widths = [max(len(row[i]) if i < len(row) else 0 for row in cells) for i in range(max(map(len, cells)))]
    return ["  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip() for row in cells]


def blocks(markdown: str) -> list[tuple[str, list[str]]]:
    """Split Markdown into ('pre', raw lines) for ``` fences and tables and ('text', lines) for paragraphs."""
    out: list[tuple[str, list[str]]] = []
    text: list[str] = []
    table: list[str] = []
    fence: list[str] | None = None

    def flush() -> None:
        nonlocal text, table
        if text:
            out.append(("text", text))
        if rows := _table(table) if table else []:
            out.append(("pre", rows))
        text, table = [], []

    for line in markdown.replace("\r\n", "\n").split("\n"):
        if fence is not None:
            if _FENCE.match(line):
                if fence:
                    out.append(("pre", fence))
                fence = None
            else:
                fence.append(line)
        elif _FENCE.match(line):
            flush()
            fence = []
        elif line.strip().startswith("|"):
            if text:
                out.append(("text", text))
                text = []
            table.append(line)
        elif not line.strip():
            flush()
        else:
            if table:
                flush()
            text.append(line)
    flush()
    if fence:
        out.append(("pre", fence))  # unterminated fence: still a code block
    return out


def _split_raw(raw: str, render: Callable[[str], str], budget: int) -> list[str]:
    """Split one over-long line so each rendered piece fits `budget` (prefers whitespace; renders after cutting,
    so entities and tags are never cut)."""
    pieces: list[str] = []
    rest = raw
    while rest:
        if len(render(rest)) <= budget:
            pieces.append(rest)
            break
        lo, hi = 1, len(rest)  # longest prefix that still fits
        while lo < hi:
            mid = (lo + hi + 1) // 2
            lo, hi = (mid, hi) if len(render(rest[:mid])) <= budget else (lo, mid - 1)
        space = rest.rfind(" ", 1, lo + 1)
        cut = space if space > lo // 2 else lo
        pieces.append(rest[:cut])
        rest = rest[cut:].lstrip(" ")
    return pieces


def _pre(lines: list[str]) -> str:
    return f"<pre>{_escape(chr(10).join(lines))}</pre>"


def _block_pieces(kind: str, lines: list[str], limit: int) -> list[str]:
    if kind == "pre":
        budget = limit - len(_pre([]))
        flat = [p for ln in lines for p in (_split_raw(ln, _escape, budget) if len(_escape(ln)) > budget else [ln])]
        pieces, cur = [], []
        for ln in flat:
            if cur and len(_pre([*cur, ln])) > limit:
                pieces.append(_pre(cur))
                cur = []
            cur.append(ln)
        return [*pieces, _pre(cur)]
    rendered = [_line(ln) for ln in lines]
    joined = "\n".join(rendered)
    if len(joined) <= limit:
        return [joined]
    out: list[str] = []
    for ln, html_line in zip(lines, rendered, strict=True):
        out += [html_line] if len(html_line) <= limit else [_line(p) for p in _split_raw(ln, _line, limit)]
    return out


def to_html(markdown: str) -> str:
    """The whole message as Telegram HTML (no length limit; see `chunks`)."""
    return "\n\n".join(
        _pre(lines) if kind == "pre" else "\n".join(_line(ln) for ln in lines) for kind, lines in blocks(markdown)
    )


def chunks(markdown: str, limit: int = MAX_CHARS) -> list[str]:
    """Telegram HTML messages of at most `limit` characters, split between blocks or lines (never inside <pre>)."""
    pieces: list[tuple[str, str]] = []  # (html, separator before it)
    for i, (kind, lines) in enumerate(blocks(markdown)):
        for j, piece in enumerate(_block_pieces(kind, lines, limit)):
            pieces.append((piece, ("\n\n" if i else "") if j == 0 else "\n"))
    out: list[str] = []
    cur = ""
    for piece, sep in pieces:
        if cur and len(cur) + len(sep) + len(piece) > limit:
            out.append(cur)
            cur = piece
        else:
            cur = cur + sep + piece if cur else piece
    if cur:
        out.append(cur)
    return out


def plain_text(html_text: str) -> str:
    """Fallback when Telegram rejects our HTML: the same text without tags."""
    return html.unescape(_TAG.sub("", html_text))


# ---------------------------------------------------------------- Bot API


def stored_token() -> str | None:
    try:
        return credentials.get(credentials.TELEGRAM_BOT_TOKEN)
    except Exception as err:  # noqa: BLE001 - e.g. no OS keyring on a headless machine
        raise TelegramError(f"Credential store unavailable ({type(err).__name__}): run `fbm notify setup`.") from None


def _payload(r: httpx.Response) -> dict[str, Any]:
    try:
        data = r.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _post(client: httpx.Client, token: str, payload: dict[str, Any], sleep: Sleep) -> None:
    url = f"{API}/bot{token}/sendMessage"
    for attempt in range(1, ATTEMPTS + 1):
        last = attempt == ATTEMPTS
        try:
            r = client.post(url, json=payload)
        except httpx.HTTPError as err:  # message may carry the URL (and so the token): report the type only
            if not last:
                sleep(2 ** (attempt - 1))
                continue
            raise TelegramError(
                f"Telegram unreachable ({type(err).__name__}); check the internet connection."
            ) from None
        data = _payload(r)
        if r.status_code == 200 and data.get("ok"):
            return
        desc = str(data.get("description") or r.reason_phrase or "no detail")[:200]
        if r.status_code == 429:
            wait = float((data.get("parameters") or {}).get("retry_after") or 1)
            if not last and wait <= MAX_RETRY_AFTER_S:
                sleep(wait)
                continue
            raise TelegramError(f"Telegram rate limit (429): retry after {wait:.0f}s.")
        if r.status_code >= 500:
            if not last:
                sleep(2 ** (attempt - 1))
                continue
            raise TelegramError(f"Telegram server error {r.status_code}: {desc}")
        if r.status_code == 401:
            raise TelegramError(
                "Telegram rejected the bot token (401 Unauthorized): run `fbm notify setup` with the token from "
                "@BotFather."
            )
        if r.status_code == 400 and "parse entities" in desc.lower() and payload.get("parse_mode"):
            payload = {k: v for k, v in payload.items() if k != "parse_mode"} | {"text": plain_text(payload["text"])}
            continue
        if r.status_code in (400, 403):
            raise TelegramError(
                f"Telegram refused the message ({r.status_code}: {desc}). Check [notify] telegram_chat_id and that "
                "you sent /start to your bot."
            )
        raise TelegramError(f"Telegram error {r.status_code}: {desc}")


def send(
    text: str,
    *,
    silent: bool = False,
    settings: Settings | None = None,
    token: str | None = None,
    client: httpx.Client | None = None,
    sleep: Sleep = time.sleep,
) -> int:
    """Send Markdown `text` to the owner's chat (split if long). silent = no notification sound.
    Returns the number of Telegram messages sent; raises TelegramError."""
    settings = settings or load_settings()
    chat_id = settings.telegram_chat_id.strip()
    if not chat_id:
        raise TelegramError("No Telegram chat id: run `fbm notify setup` ([notify] telegram_chat_id).")
    token = token or stored_token()
    if not token:
        raise TelegramError("No Telegram bot token stored: run `fbm notify setup`.")
    parts = chunks(text)
    if not parts:
        raise TelegramError("Nothing to send (empty message).")
    own = client is None
    http = client or httpx.Client(timeout=TIMEOUT_S)
    try:
        for i, part in enumerate(parts):
            payload = {
                "chat_id": chat_id,
                "text": part,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": silent,
            }
            try:
                _post(http, token, payload, sleep)
            except TelegramError as err:
                if i:
                    raise TelegramError(f"{err} (after {i} of {len(parts)} parts were sent)") from None
                raise
    finally:
        if own:
            http.close()
    return len(parts)


def detect_chat_ids(token: str, client: httpx.Client | None = None) -> list[dict[str, str]]:
    """Chats that recently messaged the bot, from ONE getUpdates call (explicit setup helper only; it does not
    confirm updates, but a concurrent poller such as OpenClaw may see a one-off 409 conflict)."""
    own = client is None
    http = client or httpx.Client(timeout=TIMEOUT_S)
    try:
        r = http.get(f"{API}/bot{token}/getUpdates", params={"timeout": 0, "limit": 50})
    except httpx.HTTPError as err:
        raise TelegramError(f"Telegram unreachable ({type(err).__name__}).") from None
    finally:
        if own:
            http.close()
    data = _payload(r)
    if r.status_code == 401:
        raise TelegramError("Telegram rejected the bot token (401 Unauthorized).")
    if r.status_code == 409:
        raise TelegramError(
            "Another program (e.g. OpenClaw) is reading this bot's updates (409). Use OpenClaw's pairing message or "
            "open https://api.telegram.org/bot<token>/getUpdates while it is stopped."
        )
    if not data.get("ok"):
        raise TelegramError(f"Telegram error {r.status_code}: {str(data.get('description') or '')[:200]}")
    chats: dict[str, dict[str, str]] = {}
    for update in data.get("result") or []:
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = (update.get(key) or {}).get("chat") or {}
            if "id" in chat:
                name = chat.get("username") or " ".join(
                    x for x in (chat.get("first_name"), chat.get("last_name"), chat.get("title")) if x
                )
                chats[str(chat["id"])] = {"chat_id": str(chat["id"]), "name": name, "type": str(chat.get("type", ""))}
    return list(chats.values())
