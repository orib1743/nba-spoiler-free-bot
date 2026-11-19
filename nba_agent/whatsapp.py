"""Official WhatsApp Cloud API: receive messages (webhook) and send replies.

Env vars (put them in .env):
  WHATSAPP_TOKEN            access token for the Cloud API
  WHATSAPP_PHONE_NUMBER_ID  the bot's phone number ID (not the number itself)
  WHATSAPP_VERIFY_TOKEN     any secret string; you type the same one in Meta's webhook settings
  WHATSAPP_APP_SECRET       the Meta app secret; used to check that webhooks really come from Meta
  WHATSAPP_TEMPLATE         approved template for messages outside the 24h window (default nba_digest)

Meta's rule: free-form text can be sent only within 24 hours of the user's last
message to the bot. Outside that window, only approved templates are delivered.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

from . import store

log = logging.getLogger("nba_agent.whatsapp")
WINDOW = timedelta(hours=23, minutes=30)  # a little inside Meta's 24h, to be safe


class WhatsAppError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get("WHATSAPP_TOKEN") and os.environ.get("WHATSAPP_PHONE_NUMBER_ID"))


def digits(phone: str) -> str:
    return "".join(ch for ch in phone if ch.isdigit())


def verify_signature(raw_body: bytes, header: str | None) -> bool:
    secret = os.environ.get("WHATSAPP_APP_SECRET")
    if not secret:
        log.warning("WHATSAPP_APP_SECRET not set: rejecting webhook (can't verify it came from Meta)")
        return False
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def parse_incoming(payload: dict) -> list[dict]:
    """Text messages in a webhook payload: [{id, from, text, name}]. Other types get a placeholder."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in value.get("contacts", [])}
            for m in value.get("messages", []):
                if m.get("type") == "text":
                    text = m["text"]["body"]
                elif m.get("type") == "button":
                    text = m.get("button", {}).get("text", "")
                else:
                    text = f"[the user sent a {m.get('type')} message, which this bot can't read]"
                out.append({"id": m["id"], "from": m["from"], "text": text, "name": names.get(m["from"], "")})
            for st in value.get("statuses", []):
                if st.get("status") == "failed":
                    log.warning("WhatsApp delivery failed: %s", st.get("errors"))
    return out


def _post(body: dict) -> dict:
    token, phone_id = os.environ.get("WHATSAPP_TOKEN"), os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    if not token or not phone_id:
        raise WhatsAppError("Set WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID in .env.")
    version = os.environ.get("WHATSAPP_API_VERSION", "v27.0")
    r = httpx.post(f"https://graph.facebook.com/{version}/{phone_id}/messages",
                   json={"messaging_product": "whatsapp", "recipient_type": "individual", **body},
                   headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if r.status_code >= 300:
        raise WhatsAppError(f"WhatsApp API HTTP {r.status_code}: {r.text[:400]}")
    return r.json()


def send_text(to: str, text: str) -> None:
    """Free-form text (only delivered inside the 24h window). Splits long replies."""
    chunks, rest = [], text
    while rest:
        cut = rest[:4000]
        if len(rest) > 4000 and "\n" in cut:
            cut = cut[:cut.rfind("\n")]
        chunks.append(cut)
        rest = rest[len(cut):].lstrip("\n")
    for chunk in chunks:
        _post({"to": digits(to), "type": "text", "text": {"body": chunk, "preview_url": False}})


def send_template(to: str, text: str) -> None:
    """Approved template with the message as its single {{1}} parameter (flattened to one line)."""
    flat = " | ".join(line.strip() for line in text.splitlines() if line.strip())[:1000]
    _post({"to": digits(to), "type": "template", "template": {
        "name": os.environ.get("WHATSAPP_TEMPLATE", "nba_digest"),
        "language": {"code": os.environ.get("WHATSAPP_TEMPLATE_LANG", "en")},
        "components": [{"type": "body", "parameters": [{"type": "text", "text": flat}]}]}})


def window_open(user_id: int = 1) -> bool:
    last = store.get_last_inbound(user_id)
    return bool(last and datetime.now(timezone.utc) - last < WINDOW)


def deliver(user_id: int, to: str, text: str) -> str:
    """Morning message: plain text if the 24h window is open, otherwise the template."""
    if window_open(user_id):
        send_text(to, text)
        return "sent via WhatsApp (text)"
    send_template(to, text)
    return "sent via WhatsApp (template)"
