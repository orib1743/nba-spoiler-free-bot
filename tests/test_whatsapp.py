import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from nba_agent import chat, store, whatsapp, web

SECRET = "test-app-secret"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test gets its own throwaway database and fake credentials."""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.delenv("WHATSAPP_ALLOWED_NUMBERS", raising=False)


@pytest.fixture
def local():  # the settings page, opened in a browser on this PC
    return TestClient(web.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))


@pytest.fixture
def tunnel():  # ngrok: connects from loopback but carries its public hostname
    return TestClient(web.app, base_url="https://my-bot.ngrok-free.app", client=("127.0.0.1", 50000))


@pytest.fixture
def internet():  # a stranger hitting a cloud server directly, faking a local hostname
    return TestClient(web.app, base_url="http://127.0.0.1:8000", client=("203.0.113.9", 40000))


def signed(payload: dict) -> tuple[bytes, dict]:
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"x-hub-signature-256": sig, "content-type": "application/json"}


def text_msg(msg_id="wamid.1", sender="15551234567", body="hi"):
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": sender, "profile": {"name": "Fan"}}],
        "messages": [{"id": msg_id, "from": sender, "type": "text", "text": {"body": body}}]}}]}]}


@pytest.fixture
def fake_agent(monkeypatch):
    sent, asked = [], []
    monkeypatch.setattr(chat, "send", lambda text, channel="web": asked.append((text, channel)) or {"reply": f"echo: {text}", "settings_changed": False})
    monkeypatch.setattr(whatsapp, "send_text", lambda to, text: sent.append((to, text)))
    return asked, sent


def test_tunnel_exposes_only_the_webhook(tunnel, local):
    assert tunnel.get("/").status_code == 404
    assert tunnel.get("/api/settings").status_code == 404
    assert tunnel.post("/api/chat", json={"text": "x"}).status_code == 404
    assert local.get("/api/history").status_code == 200


def test_faked_local_hostname_from_internet_is_blocked(internet):
    assert internet.get("/api/settings").status_code == 404
    assert internet.get("/healthz").status_code == 200


def test_admin_password_opens_settings_remotely(internet, monkeypatch):
    monkeypatch.setenv("NBA_AGENT_ADMIN_PASSWORD", "s3cret")
    assert internet.get("/api/settings").status_code == 401  # browser shows a login box
    assert internet.get("/api/settings", auth=("admin", "wrong")).status_code == 401
    assert internet.get("/api/settings", auth=("admin", "s3cret")).status_code == 200


def test_webhook_verification(tunnel):
    ok = tunnel.get("/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    bad = tunnel.get("/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"})
    assert bad.status_code == 403


def test_unsigned_or_forged_webhook_is_rejected(tunnel, fake_agent):
    raw, headers = signed(text_msg())
    assert tunnel.post("/whatsapp/webhook", content=raw, headers={**headers, "x-hub-signature-256": "sha256=00"}).status_code == 401
    assert tunnel.post("/whatsapp/webhook", content=raw, headers={"content-type": "application/json"}).status_code == 401
    assert fake_agent == ([], [])


def test_message_reaches_agent_and_reply_goes_back(tunnel, fake_agent):
    asked, sent = fake_agent
    raw, headers = signed(text_msg(body="which games tonight?"))
    assert tunnel.post("/whatsapp/webhook", content=raw, headers=headers).status_code == 200
    assert asked == [("which games tonight?", "whatsapp")]
    assert sent == [("15551234567", "echo: which games tonight?")]
    s = store.get_settings()  # first sender becomes the owner, morning message switches to WhatsApp
    assert s.phone == "+15551234567" and s.sender.type == "meta"
    assert whatsapp.window_open(1)


def test_duplicate_delivery_is_answered_once(tunnel, fake_agent):
    raw, headers = signed(text_msg(msg_id="wamid.same"))
    tunnel.post("/whatsapp/webhook", content=raw, headers=headers)
    tunnel.post("/whatsapp/webhook", content=raw, headers=headers)
    assert len(fake_agent[1]) == 1


def test_strangers_are_ignored_once_owner_is_set(tunnel, fake_agent):
    raw, headers = signed(text_msg(msg_id="a", sender="15551234567"))
    tunnel.post("/whatsapp/webhook", content=raw, headers=headers)
    raw, headers = signed(text_msg(msg_id="b", sender="15550001111", body="let me in"))
    tunnel.post("/whatsapp/webhook", content=raw, headers=headers)
    assert [to for to, _ in fake_agent[1]] == ["15551234567"]


def test_morning_message_uses_template_outside_window(monkeypatch):
    calls = []
    monkeypatch.setattr(whatsapp, "send_text", lambda to, t: calls.append("text"))
    monkeypatch.setattr(whatsapp, "send_template", lambda to, t: calls.append("template"))
    whatsapp.deliver(1, "+15551234567", "picks")
    store.mark_inbound(1)
    whatsapp.deliver(1, "+15551234567", "picks")
    assert calls == ["template", "text"]
