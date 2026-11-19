"""Morning-message delivery. Interchangeable senders:

console    prints the message (default; for trying things out)
telegram   your Telegram bot chat (free, no time-window rules)
meta       official WhatsApp Cloud API. Within 24h of your last message to the
           bot the digest goes as plain text; otherwise as the {{1}} parameter
           of an approved template.
callmebot  free WhatsApp sender, personal use only:
           https://www.callmebot.com/blog/free-api-whatsapp-messages/
"""
from __future__ import annotations

import httpx

from . import whatsapp
from .store import Settings


class SendError(RuntimeError):
    pass


def send(s: Settings, text: str) -> str:
    kind = s.sender.type
    if kind == "console":
        print("\n----- Morning message (dry run) -----\n" + text + "\n-----")
        return "printed to console (dry run)"
    if kind == "telegram":
        from . import telegram  # imported here: telegram -> chat -> tools -> senders would loop
        if not s.telegram_chat_id:
            raise SendError("Send your Telegram bot a message first so it knows where to reach you.")
        try:
            telegram.send_text(s.telegram_chat_id, text)
        except telegram.TelegramError as e:
            raise SendError(str(e))
        return "sent via Telegram"
    if not s.phone:
        raise SendError("Set your WhatsApp phone number first.")
    if kind == "callmebot":
        return _callmebot(s, text)
    if kind == "meta":
        return _meta(s, text)
    raise SendError(f"unknown sender {kind!r}")


def _callmebot(s: Settings, text: str) -> str:
    if not s.sender.callmebot_apikey:
        raise SendError("CallMeBot API key missing.")
    r = httpx.get("https://api.callmebot.com/whatsapp.php", timeout=30,
                  params={"phone": s.phone, "text": text, "apikey": s.sender.callmebot_apikey})
    body = r.text.strip()
    if r.status_code != 200 or "error" in body.lower() or "invalid" in body.lower():
        raise SendError(f"CallMeBot HTTP {r.status_code}: {body[:300]}")
    return "sent via CallMeBot"


def _meta(s: Settings, text: str) -> str:
    try:
        return whatsapp.deliver(1, s.phone, text)
    except whatsapp.WhatsAppError as e:
        raise SendError(str(e))
