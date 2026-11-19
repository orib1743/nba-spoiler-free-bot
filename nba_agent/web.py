"""Settings website + API. Run with:  python run.py web"""
from __future__ import annotations

import asyncio
import base64
import json
import secrets
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import anthropic
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import chat, digest, espn, scheduler, senders, store, telegram, whatsapp

STATIC = Path(__file__).resolve().parent / "static"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
# The HTTP libraries log every request URL at INFO, and Telegram's URLs contain the bot token.
for noisy in ("httpx", "httpx2", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(scheduler.loop())
    tg_stop = threading.Event()
    if telegram.configured():
        threading.Thread(target=telegram.poll_forever, args=(tg_stop,), daemon=True, name="telegram").start()
    yield
    tg_stop.set()
    task.cancel()


app = FastAPI(title="NBA Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
log = logging.getLogger("nba_agent.web")

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
LOOPBACK_IPS = {"127.0.0.1", "::1"}
PUBLIC_PATHS = {"/whatsapp/webhook", "/healthz"}


def _is_local(request: Request) -> bool:
    """Opened on this machine: the connection comes from loopback AND the browser
    used a local hostname. (A tunnel connects from loopback but carries its public
    hostname; a direct internet request can fake the hostname but not the IP.)"""
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
    ip = request.client.host if request.client else ""
    return ip in LOOPBACK_IPS and host in LOCAL_HOSTS


def _password_ok(request: Request) -> bool:
    password = os.environ.get("NBA_AGENT_ADMIN_PASSWORD")
    auth = request.headers.get("authorization", "")
    if not password or not auth.lower().startswith("basic "):
        return False
    try:
        _, _, given = base64.b64decode(auth[6:]).decode().partition(":")
    except Exception:
        return False
    return secrets.compare_digest(given.encode(), password.encode())


@app.middleware("http")
async def guard(request: Request, call_next):
    """The settings page and chat answer on this machine. From anywhere else they
    need NBA_AGENT_ADMIN_PASSWORD (any username); without one set, only the
    WhatsApp webhook and the health check are reachable."""
    if request.url.path in PUBLIC_PATHS or _is_local(request) or _password_ok(request):
        return await call_next(request)
    if os.environ.get("NBA_AGENT_ADMIN_PASSWORD"):
        return PlainTextResponse("Login required", status_code=401,
                                 headers={"WWW-Authenticate": 'Basic realm="NBA Agent"'})
    return PlainTextResponse("Not found", status_code=404)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/whatsapp/webhook")
def whatsapp_verify(request: Request):
    """Meta calls this once when you save the webhook URL in the app dashboard."""
    q = request.query_params
    expected = os.environ.get("WHATSAPP_VERIFY_TOKEN")
    if q.get("hub.mode") == "subscribe" and expected and q.get("hub.verify_token") == expected:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "verification failed")


@app.post("/whatsapp/webhook")
async def whatsapp_incoming(request: Request, background: BackgroundTasks):
    raw = await request.body()
    if not whatsapp.verify_signature(raw, request.headers.get("x-hub-signature-256")):
        raise HTTPException(401, "bad signature")
    for msg in whatsapp.parse_incoming(json.loads(raw or b"{}")):
        if store.first_time_seen(msg["id"]):
            background.add_task(_handle_whatsapp, msg)
    return {"ok": True}  # answer Meta right away; the agent replies in the background


def _allowed(sender: str, s: store.Settings) -> bool:
    allowed = {whatsapp.digits(n) for n in os.environ.get("WHATSAPP_ALLOWED_NUMBERS", "").split(",") if n.strip()}
    if s.phone:
        allowed.add(whatsapp.digits(s.phone))
    return not allowed or sender in allowed


def _handle_whatsapp(msg: dict) -> None:
    s = store.get_settings()
    if not _allowed(msg["from"], s):
        log.warning("ignored WhatsApp message from an unknown number")
        return
    store.mark_inbound(1)
    if not s.phone or s.sender.type == "console":
        # The first person to message the bot (or the allowed number) becomes its owner.
        # If no delivery channel is set up yet, the morning message comes here.
        s.phone = s.phone or "+" + msg["from"]
        if s.sender.type == "console":
            s.sender.type = "meta"
        store.save_settings(s)
    try:
        reply = chat.send(msg["text"][:4000], channel="whatsapp")["reply"]
    except Exception as e:
        log.exception("WhatsApp turn failed")
        reply = f"Sorry, I hit a problem and couldn't answer ({type(e).__name__}). Try again in a minute."
    try:
        whatsapp.send_text(msg["from"], reply)
    except whatsapp.WhatsAppError:
        log.exception("couldn't send WhatsApp reply")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/settings")
def get_settings():
    return {"settings": store.as_dict(store.get_settings()), "default_night": digest.game_night().isoformat()}


@app.put("/api/settings")
def put_settings(s: store.Settings):
    store.save_settings(s)
    return {"ok": True}


@app.get("/api/teams")
def teams():
    return espn.teams()


@app.get("/api/teams/{team_id}/roster")
def roster(team_id: str):
    return espn.roster(team_id)


class PreviewReq(BaseModel):
    settings: store.Settings
    night: date | None = None


@app.post("/api/preview")
def preview(req: PreviewReq):
    night = req.night or digest.game_night()
    text, _ = digest.build(night, store.as_dict(req.settings))
    return {"night": night.isoformat(), "text": text}


@app.post("/api/send-test")
def send_test():
    s = store.get_settings()
    night = digest.game_night()
    text, _ = digest.build(night, store.as_dict(s))
    try:
        info = senders.send(s, text)
    except senders.SendError as e:
        store.log_send(1, night.isoformat(), "test", False, text, str(e))
        raise HTTPException(400, str(e))
    store.log_send(1, night.isoformat(), "test", True, text)
    return {"ok": True, "info": info, "text": text}


@app.get("/api/history")
def history():
    return store.history()


class ChatReq(BaseModel):
    text: str


@app.get("/api/chat")
def chat_log():
    return {"messages": store.chat_log(), "configured": bool(os.environ.get("ANTHROPIC_API_KEY"))}


@app.post("/api/chat")
def chat_send(req: ChatReq):
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "Empty message")
    try:
        return chat.send(text[:4000])
    except chat.ChatError as e:
        raise HTTPException(400, str(e))
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"Claude API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "Couldn't reach the Claude API. Check your internet connection.")


@app.post("/api/chat/new")
def chat_new():
    chat.new_chat()
    return {"ok": True}
