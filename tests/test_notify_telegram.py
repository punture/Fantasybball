"""Telegram delivery: Markdown subset -> Telegram HTML, chunking, and the Bot API client (httpx.MockTransport)."""

import json
import re

import httpx
import pytest

from fbm.notify import telegram
from fbm.settings import Settings

TOKEN = "123456789:" + "A" * 35
SETTINGS = Settings(telegram_chat_id="4242")


def test_markdown_subset_to_html_escapes_everything_else():
    md = (
        "# Title <x>\n"
        "**Bold** & *italic* with `a<b && c` and [docs](https://x.com/a?b=1&c=2)\n"
        "- bullet one\n"
        "  - nested **two**\n"
        "2 * 3 * 4 and fg_pct stay literal\n"
        "\n"
        "```\nif a < b: print('&')\n```"
    )
    out = telegram.to_html(md)
    assert out.startswith("<b>Title &lt;x&gt;</b>\n")
    assert "<b>Bold</b> &amp; <i>italic</i> with <code>a&lt;b &amp;&amp; c</code>" in out
    assert '<a href="https://x.com/a?b=1&amp;c=2">docs</a>' in out
    assert "• bullet one\n  • nested <b>two</b>" in out
    assert "2 * 3 * 4 and fg_pct stay literal" in out
    assert out.endswith("<pre>if a &lt; b: print('&amp;')</pre>")


def test_tables_become_aligned_pre_blocks():
    out = telegram.to_html("| Cat | P(win) |\n|---|---|\n| **PTS** | 81% |\n| FG% | 5% |")
    assert out == "<pre>Cat  P(win)\nPTS  81%\nFG%  5%</pre>"


def test_unterminated_fence_and_blank_blocks():
    assert telegram.to_html("```\nx < 1") == "<pre>x &lt; 1</pre>"
    assert telegram.to_html("a\n\n```\n```\n\nb") == "a\n\nb"
    assert telegram.chunks("   \n\n") == []


def test_chunks_respect_the_limit_and_never_split_inside_pre():
    para = "\n".join(f"- line {i} with **bold** & stuff" for i in range(40))
    code = "```\n" + "\n".join(f"row {i} <{i}>" for i in range(60)) + "\n```"
    md = "\n\n".join([para, code, para])
    parts = telegram.chunks(md, limit=500)
    assert len(parts) > 3 and all(len(p) <= 500 for p in parts)
    for p in parts:
        assert p.count("<pre>") == p.count("</pre>")  # every <pre> opened in a part is closed in it
        assert p.count("<b>") == p.count("</b>")
    joined = "\n".join(parts)
    assert "row 59 &lt;59&gt;" in joined and "line 39" in joined


def test_chunks_split_a_single_long_line_without_cutting_entities():
    md = "a&b " * 400  # one line; every & becomes &amp; (5 characters)
    parts = telegram.chunks(md, limit=300)
    assert len(parts) > 1 and all(len(p) <= 300 for p in parts)
    assert all(re.fullmatch(r"(?:[ab ]|&amp;)+", p) for p in parts)  # no entity cut in half
    assert sum(p.count("&amp;") for p in parts) == 400


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_send_posts_html_with_flags():
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": {}})

    n = telegram.send("**Hi** <you>", silent=True, settings=SETTINGS, token=TOKEN, client=_client(handler))
    assert n == 1
    path, payload = seen[0]
    assert path == f"/bot{TOKEN}/sendMessage"
    assert payload == {
        "chat_id": "4242",
        "text": "<b>Hi</b> &lt;you&gt;",
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": True,
    }


def test_send_retries_429_after_retry_after_and_5xx():
    calls, sleeps = [], []
    replies = [
        httpx.Response(429, json={"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 7}}),
        httpx.Response(502, json={"ok": False, "description": "Bad Gateway"}),
        httpx.Response(200, json={"ok": True}),
    ]

    def handler(request):
        calls.append(request)
        return replies[len(calls) - 1]

    telegram.send("x", settings=SETTINGS, token=TOKEN, client=_client(handler), sleep=sleeps.append)
    assert len(calls) == 3 and sleeps == [7.0, 2]


def test_send_gives_up_on_a_long_retry_after():
    def handler(request):
        return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 3600}})

    with pytest.raises(telegram.TelegramError, match="retry after 3600s"):
        telegram.send("x", settings=SETTINGS, token=TOKEN, client=_client(handler), sleep=lambda s: None)


@pytest.mark.parametrize(
    ("status", "desc", "match"),
    [
        (401, "Unauthorized", "rejected the bot token"),
        (400, "Bad Request: chat not found", "telegram_chat_id"),
        (403, "Forbidden: bot was blocked by the user", "/start"),
    ],
)
def test_send_errors_are_actionable_and_never_show_the_token(status, desc, match):
    def handler(request):
        return httpx.Response(status, json={"ok": False, "error_code": status, "description": desc})

    with pytest.raises(telegram.TelegramError, match=match) as err:
        telegram.send("x", settings=SETTINGS, token=TOKEN, client=_client(handler), sleep=lambda s: None)
    assert TOKEN not in str(err.value) and "A" * 35 not in str(err.value)


def test_network_errors_retry_then_fail_without_the_url():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError(f"cannot reach {request.url}")

    with pytest.raises(telegram.TelegramError, match="ConnectError") as err:
        telegram.send("x", settings=SETTINGS, token=TOKEN, client=_client(handler), sleep=lambda s: None)
    assert len(calls) == telegram.ATTEMPTS and TOKEN not in str(err.value) and err.value.__cause__ is None


def test_html_parse_error_falls_back_to_plain_text():
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        if "parse_mode" in payloads[-1]:
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: can't parse entities"})
        return httpx.Response(200, json={"ok": True})

    telegram.send("**a** & b", settings=SETTINGS, token=TOKEN, client=_client(handler))
    assert payloads[-1]["text"] == "a & b" and "parse_mode" not in payloads[-1]


def test_send_needs_chat_id_and_token(monkeypatch):
    with pytest.raises(telegram.TelegramError, match="chat id"):
        telegram.send("x", settings=Settings(), token=TOKEN)
    monkeypatch.setattr("fbm.credentials.get", lambda name: None)
    with pytest.raises(telegram.TelegramError, match="bot token"):
        telegram.send("x", settings=SETTINGS)


def test_long_message_is_sent_in_parts_and_reports_partial_failure():
    sent = []

    def handler(request):
        sent.append(1)
        return httpx.Response(200 if len(sent) == 1 else 400, json={"ok": len(sent) == 1, "description": "nope"})

    body = "\n\n".join("p" * 3000 for _ in range(3))
    with pytest.raises(telegram.TelegramError, match="after 1 of 3 parts"):
        telegram.send(body, settings=SETTINGS, token=TOKEN, client=_client(handler))


def test_detect_chat_ids_reads_updates_once():
    def handler(request):
        assert request.url.path.endswith("/getUpdates")
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {"message": {"chat": {"id": 99, "type": "private", "first_name": "Ann"}}},
                    {"message": {"chat": {"id": 99, "type": "private", "first_name": "Ann"}}},
                    {"channel_post": {"chat": {"id": -100, "type": "channel", "title": "League"}}},
                ],
            },
        )

    chats = telegram.detect_chat_ids(TOKEN, _client(handler))
    assert chats == [
        {"chat_id": "99", "name": "Ann", "type": "private"},
        {"chat_id": "-100", "name": "League", "type": "channel"},
    ]

    def conflict(request):
        return httpx.Response(409, json={"ok": False, "description": "Conflict: terminated by other getUpdates"})

    with pytest.raises(telegram.TelegramError, match="OpenClaw"):
        telegram.detect_chat_ids(TOKEN, _client(conflict))
