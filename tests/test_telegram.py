import threading

import pytest

from nba_agent import chat, senders, store, telegram, tools


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fake")
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)


@pytest.fixture
def bot(monkeypatch):
    """Fake Telegram API + fake agent. Records what the bot sends and what the agent is asked."""
    sent, asked = [], []

    def fake_call(method, http_timeout=30, **params):
        if method == "sendMessage":
            sent.append((params["chat_id"], params["text"]))
        return True

    monkeypatch.setattr(telegram, "call", fake_call)
    monkeypatch.setattr(chat, "send", lambda text, channel="web": asked.append((text, channel)) or {"reply": "pong", "settings_changed": False})
    monkeypatch.setattr(chat, "new_chat", lambda: asked.append(("<new chat>", None)))
    return sent, asked


def update(text, user=111, chat_type="private"):
    return {"update_id": 1, "message": {"message_id": 5, "text": text,
                                        "from": {"id": user}, "chat": {"id": user, "type": chat_type}}}


def test_first_message_claims_bot_and_becomes_delivery_channel(bot):
    sent, asked = bot
    telegram.handle(update("hi"))
    assert asked == [("hi", "telegram")] and sent == [(111, "pong")]
    s = store.get_settings()
    assert s.telegram_chat_id == "111" and s.sender.type == "telegram"


def test_start_command_opens_conversation(bot):
    telegram.handle(update("/start"))
    assert bot[1][0][0].startswith("Hi!")


def test_strangers_get_private_notice(bot):
    sent, asked = bot
    telegram.handle(update("hi", user=111))
    telegram.handle(update("let me in", user=222))
    assert asked == [("hi", "telegram")]
    assert sent[-1] == (222, "Sorry, this is a private bot.")


def test_groups_are_ignored(bot):
    telegram.handle(update("hi", chat_type="group"))
    assert bot == ([], [])


def test_new_command_resets_conversation(bot):
    telegram.handle(update("/new"))
    assert bot[1] == [("<new chat>", None)]


def test_existing_whatsapp_delivery_is_not_switched(bot):
    s = store.get_settings(); s.phone, s.sender.type = "+15551234567", "meta"; store.save_settings(s)
    telegram.handle(update("hi"))
    s = store.get_settings()
    assert s.telegram_chat_id == "111" and s.sender.type == "meta"


def test_agent_can_switch_delivery_channel(bot, monkeypatch):
    monkeypatch.setattr(tools.espn, "teams", lambda: [])
    assert tools.update_settings(delivery_channel="telegram")["notes"] == [
        "Telegram isn't connected: the user has to message the Telegram bot first."]
    telegram.handle(update("hi"))
    s = store.get_settings(); s.sender.type = "console"; store.save_settings(s)
    assert tools.update_settings(delivery_channel="telegram")["ok"]
    assert store.get_settings().sender.type == "telegram"


def test_morning_message_via_telegram(bot):
    s = store.Settings(telegram_chat_id="111", sender={"type": "telegram"})
    assert senders.send(s, "🏀 picks") == "sent via Telegram"
    assert bot[0] == [("111", "🏀 picks")]


def test_poller_remembers_offset(monkeypatch):
    seen, stop = [], threading.Event()
    batches = [[{"update_id": 41, "message": {}}, {"update_id": 42, "message": {}}], []]

    def fake_call(method, http_timeout=30, **params):
        if method == "getUpdates":
            seen.append(params.get("offset"))
            if len(seen) == 2:
                stop.set()
            return batches[len(seen) - 1]
        return {"username": "test_bot"}

    monkeypatch.setattr(telegram, "call", fake_call)
    monkeypatch.setattr(telegram, "_safe_handle", lambda u: None)
    monkeypatch.setattr(telegram._pool, "submit", lambda fn, u: fn(u))
    telegram.poll_forever(stop)
    assert seen == [None, 43] and store.get_kv("telegram_offset") == "42"


def test_bold_renders_and_html_is_escaped():
    assert telegram.to_html("the **matchup rule** stays <15 & ok") == "the <b>matchup rule</b> stays &lt;15 &amp; ok"


def test_falls_back_to_plain_text_if_formatting_rejected(monkeypatch):
    sent = []

    def fake_call(method, http_timeout=30, **p):
        if p.get("parse_mode"):
            raise telegram.TelegramError("can't parse entities")
        sent.append(p["text"])

    monkeypatch.setattr(telegram, "call", fake_call)
    telegram.send_text(1, "**hi** there")
    assert sent == ["hi there"]


def test_long_messages_are_split(monkeypatch):
    chunks = []
    monkeypatch.setattr(telegram, "call", lambda m, http_timeout=30, **p: chunks.append(p["text"]))
    telegram.send_text(1, ("line\n" * 1500).strip())
    assert len(chunks) == 3 and all(len(c) <= 4000 for c in chunks)
