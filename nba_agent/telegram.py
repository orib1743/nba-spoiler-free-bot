"""Telegram bot: chat with the agent and receive the morning message.

Uses long polling (the app asks Telegram for new messages), so nothing on this
PC is exposed to the internet: no tunnel, no webhook.

Env vars (.env):
  TELEGRAM_BOT_TOKEN         from @BotFather (/newbot)
  TELEGRAM_ALLOWED_USER_IDS  optional, comma-separated Telegram user ids allowed to use the bot.
                             If empty, the first person to message the bot becomes its owner.
"""
from __future__ import annotations

import html
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import chat, store

log = logging.getLogger("nba_agent.telegram")
API = "https://api.telegram.org/bot{token}/{method}"
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tg")


class TelegramError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN"))


def call(method: str, http_timeout: float = 30, **params) -> object:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise TelegramError("Set TELEGRAM_BOT_TOKEN in .env (get one from @BotFather).")
    r = httpx.post(API.format(token=token, method=method), json=params, timeout=http_timeout)
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not data.get("ok"):
        raise TelegramError(f"Telegram {method} failed: HTTP {r.status_code} {data.get('description', r.text[:200])}")
    return data["result"]


def to_html(text: str) -> str:
    """Markdown-ish model output -> Telegram HTML: escape, then **bold** -> <b>bold</b>."""
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped, flags=re.S)
    return re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", escaped, flags=re.M)  # stray markdown headers


def send_text(chat_id: str | int, text: str) -> None:
    """Send with **bold** rendered; if Telegram rejects the formatting, send it plain. Splits long messages."""
    rest = text
    while rest:
        cut = rest[:3500]  # HTML tags add length; stay under Telegram's 4096
        if len(rest) > 3500 and "\n" in cut:
            cut = cut[:cut.rfind("\n")]
        try:
            call("sendMessage", chat_id=chat_id, text=to_html(cut), parse_mode="HTML", disable_web_page_preview=True)
        except TelegramError:
            call("sendMessage", chat_id=chat_id, text=cut.replace("**", ""), disable_web_page_preview=True)
        rest = rest[len(cut):].lstrip("\n")


def _allowed(user_id: int, s: store.Settings) -> bool:
    allowed = {x.strip() for x in os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if x.strip()}
    if allowed:
        return str(user_id) in allowed
    return not s.telegram_chat_id or s.telegram_chat_id == str(user_id)


def _typing(chat_id: int, stop: threading.Event) -> None:
    while not stop.is_set():  # Telegram shows "typing…" for ~5s per call
        try:
            call("sendChatAction", http_timeout=10, chat_id=chat_id, action="typing")
        except Exception:
            pass
        stop.wait(4.5)


def handle(update: dict) -> None:
    msg = update.get("message") or update.get("edited_message")
    if not msg or msg.get("chat", {}).get("type") != "private":
        return  # ignore groups/channels
    chat_id, user_id = msg["chat"]["id"], msg["from"]["id"]
    log.info("Telegram message received; agent is replying…")
    text = msg.get("text") or f"[the user sent a non-text message ({', '.join(k for k in msg if k in ('photo', 'voice', 'sticker', 'video', 'document')) or 'other'}), which this bot can't read]"

    s = store.get_settings()
    if not _allowed(user_id, s):
        send_text(chat_id, "Sorry, this is a private bot.")
        return
    if s.telegram_chat_id != str(chat_id):
        # First contact: this chat becomes the owner's Telegram chat. If no delivery
        # channel is set up yet, the morning message comes here.
        s.telegram_chat_id = str(chat_id)
        if s.sender.type == "console":
            s.sender.type = "telegram"
        store.save_settings(s)

    if text.strip().lower() in ("/new", "/reset"):
        chat.new_chat()
        send_text(chat_id, "Started a fresh conversation 🏀")
        return
    if text.strip().lower().startswith("/start"):
        text = "Hi! (I just opened the bot.)"

    stop = threading.Event()
    threading.Thread(target=_typing, args=(chat_id, stop), daemon=True).start()
    try:
        reply = chat.send(text[:4000], channel="telegram")["reply"]
    except chat.ChatError as e:
        reply = str(e)
    except Exception as e:
        log.exception("Telegram turn failed")
        reply = f"Sorry, I hit a problem and couldn't answer ({type(e).__name__}). Try again in a minute."
    finally:
        stop.set()
    send_text(chat_id, reply)
    log.info("Telegram reply sent")


def poll_forever(stop: threading.Event) -> None:
    """Long-poll Telegram for new messages until `stop` is set."""
    try:
        me = call("getMe")
        log.info("Telegram bot @%s is listening", me.get("username"))
        call("deleteWebhook")  # polling and webhooks can't both be active
    except Exception as e:
        log.error("Telegram disabled: %s", e)
        return
    offset = store.get_kv("telegram_offset")
    while not stop.is_set():
        params = {"timeout": 50, "allowed_updates": ["message", "edited_message"]}  # Telegram holds the request up to 50s
        if offset:
            params["offset"] = int(offset) + 1
        try:
            updates = call("getUpdates", http_timeout=65, **params)
        except Exception as e:
            if "409" in str(e):
                log.warning("Another copy of this bot is running somewhere else (e.g. on your PC and in the cloud). "
                            "Telegram lets only one receive messages; stop the other copy.")
            else:
                log.warning("Telegram poll failed: %s", e)
            stop.wait(10)
            continue
        for u in updates:
            offset = str(u["update_id"])
            store.set_kv("telegram_offset", offset)  # acknowledge before handling: never process twice
            _pool.submit(_safe_handle, u)


def drain() -> None:
    """Wait for replies still being written (used before the app exits)."""
    _pool.shutdown(wait=True)


def _safe_handle(update: dict) -> None:
    try:
        handle(update)
    except Exception:
        log.exception("failed to handle Telegram update")
