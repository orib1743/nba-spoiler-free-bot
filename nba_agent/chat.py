"""The chat agent, built on Claude Managed Agents.

Anthropic runs the agent loop and keeps each conversation's history; the tools
run here, on this machine (custom tools), because they read and change the local
settings database. Setup (environment + agent) happens once and the IDs are kept
in data/agent.json; when the config below changes, the agent is updated in place.

Needs ANTHROPIC_API_KEY in the environment.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import anthropic

from . import digest, store, tools
from .paths import DATA_DIR

log = logging.getLogger("nba_agent.chat")
STATE = DATA_DIR / "agent.json"
MODEL = os.environ.get("NBA_AGENT_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("NBA_AGENT_EFFORT", "medium")
BUDGET_CENTS = os.environ.get("NBA_AGENT_CHAT_BUDGET_CENTS", "100")  # hard cap per conversation
REPLY_DEADLINE = 180  # seconds

SYSTEM = """You are the user's personal NBA companion, chatting with them like a friend who watches every game.
You also manage their "morning message": a daily, spoiler-free WhatsApp note of which games from last night are worth watching.

# Spoilers: the most important rule
The user watches games on replay and hates spoilers. Assume they have NOT watched any game unless they say so.
- Never reveal or hint at a score, winner, margin, comeback, overtime, game-winner, injury during a game, or player stat line unless the user explicitly asks for that specific information.
- "Was the Celtics game good?" / "Should I watch X?" -> answer only with get_games' verdict (worth watching or not, by their rules). Don't describe how the game went.
- "Who won?", "What was the score?", "How many did Jokic score?" are explicit requests: then use the spoiler tools and answer. If a request is ambiguous ("how did the Lakers do?"), ask "Want the result? It's a spoiler" first.
- Revealing one game never licenses revealing another. Don't volunteer extra results or standings.
- Standings include last night's results; only share them when asked.
- Talking about upcoming games, schedules, rosters, rules and settings is always fine.

# Settings
- Use get_settings to see current settings, update_settings to change them. After a change, confirm in one short line what changed.
- If update_settings returns notes (ambiguous player, unknown team), ask the user to clarify.
- The morning message goes to one channel (Telegram or WhatsApp). By default it's the chat where the user first wrote; switch with delivery_channel when asked. You can't connect a new channel yourself; the user connects one by messaging that bot.
- Rules: "matchup" = both teams are favorites and the margin entering the 4th was under N. "Close finish" rules = within N points at any point in the last M minutes (or overtime), for every game or only favorites' games. There can be several close-finish rules at once (e.g. "my teams within 5 in the last 5 min" AND "any game within 3 in the last 2 min"); a game counts if any rule matches. To change them, send the complete new list in close_rules.

# First-time setup
If the note says setup is not done, this is a new user (or one who never finished setup). Greet them in a sentence or two, say what you do, then set things up together, ONE question per message, in this order:
1. Favorite teams. If some are already saved, list them and ask whether to keep or change them.
2. The rules: explain each in one sentence with its default and ask whether the defaults are fine. "Matchup": both teams are favorites and the margin entering the 4th was under 15. "Close finish": within 5 points at any point in the last 5 minutes, or overtime, for every game. Mention they can have more than one close-finish rule (e.g. looser for their teams, stricter for everyone else).
3. Players to follow, and how much to say about them: just played/didn't play (recommended, no spoilers), minutes, or full stats (spoilers).
4. What time the morning message should arrive, and confirm the saved time zone from the note is right.
5. Whether to label why each game is recommended (a mild spoiler; default no).
Save each answer with update_settings as you go. If they answer several things at once, take them all and don't re-ask. "Skip" means keep the default.
At the end, send a short summary. When they confirm, call update_settings with onboarded=true and enabled=true, tell them the first message arrives at their chosen time, and mention they can change anything by just telling you.

# Style
Short, warm, conversational. Reply in the language the user writes in. Use team nicknames (Celtics, Lakers).
On Telegram: **bold** is fine (it's rendered); no headers, tables or [text](link). On WhatsApp: plain text, no markdown. Emoji welcome, short lines. In Telegram the user can send /new to start a fresh conversation.
Each user message starts with a bracketed note giving the channel, the current time, the latest game night and whether setup is done; use it to resolve "last night", "tonight", "tomorrow". Never mention the note itself."""

_STR = {"type": "string"}
_NIGHT = {"type": "string", "description": "US game-night date YYYY-MM-DD (Eastern). Omit for the most recent night."}
CUSTOM_TOOLS = [
    {"name": "get_settings", "description": "Current morning-message settings: teams, players, rules, send time.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "update_settings",
     "description": "Change morning-message settings. Pass only the fields to change. Teams by abbreviation or name; players by name.",
     "input_schema": {"type": "object", "properties": {
         "onboarded": {"type": "boolean", "description": "Set true when the first-time setup is finished"},
         "enabled": {"type": "boolean", "description": "Send the daily message at all"},
         "delivery_channel": {"type": "string", "enum": ["telegram", "whatsapp"],
                              "description": "Where the morning message goes (must be a connected channel)"},
         "send_time": {"type": "string", "description": "HH:MM, 24h, in the user's timezone"},
         "timezone": {"type": "string", "description": "IANA zone, e.g. Europe/London"},
         "wait_for_final_hours": {"type": "number"},
         "show_reasons": {"type": "boolean", "description": "Label why each game is recommended (slight spoiler)"},
         "player_detail": {"type": "string", "enum": ["played_only", "minutes", "stats"]},
         "add_teams": {"type": "array", "items": _STR}, "remove_teams": {"type": "array", "items": _STR},
         "add_players": {"type": "array", "items": _STR}, "remove_players": {"type": "array", "items": _STR},
         "matchup_enabled": {"type": "boolean"}, "matchup_max_margin_entering_q4": {"type": "integer"},
         "close_rules": {
             "type": "array",
             "description": "The COMPLETE list of close-finish rules (replaces the current list; send [] to turn close finishes off). "
                            "A game is recommended if any rule matches.",
             "items": {"type": "object", "properties": {
                 "minutes_left": {"type": "number", "description": "Window at the end of the 4th quarter, in minutes"},
                 "max_margin": {"type": "integer", "description": "Max margin that still counts as close (5 = 'less than 6')"},
                 "scope": {"type": "string", "enum": ["all", "favorites"], "description": "Every game, or only games with a favorite team"},
             }, "required": ["minutes_left", "max_margin", "scope"]}},
     }}},
    {"name": "get_games",
     "description": "Games on a night: matchup, local tip-off time, status, and whether each finished game is worth watching by the user's rules. Contains NO scores; safe to use anytime.",
     "input_schema": {"type": "object", "properties": {"night": _NIGHT, "team": {"type": "string", "description": "Filter to one team"}}}},
    {"name": "get_schedule", "description": "A team's upcoming games over the next N days (no spoilers).",
     "input_schema": {"type": "object", "properties": {"team": _STR, "days": {"type": "integer"}}, "required": ["team"]}},
    {"name": "get_game_result",
     "description": "SPOILER. Final score, quarter scores and top scorers of a finished game. Only after the user explicitly asked for this game's result.",
     "input_schema": {"type": "object", "properties": {"game_id": {"type": "string", "description": "From get_games"}}, "required": ["game_id"]}},
    {"name": "get_player_line",
     "description": "Whether a player played on a night. detail='full' adds minutes/points/rebounds/assists: SPOILER, only when the user explicitly asked for stats.",
     "input_schema": {"type": "object", "properties": {"player": _STR, "night": _NIGHT,
                                                       "detail": {"type": "string", "enum": ["played_only", "full"]}},
                      "required": ["player"]}},
    {"name": "get_standings", "description": "SPOILER-ish. Current standings by conference. Only when asked.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "preview_morning_message", "description": "The exact spoiler-free morning message for a night.",
     "input_schema": {"type": "object", "properties": {"night": _NIGHT}}},
    {"name": "send_morning_message_now", "description": "Send the morning message to the user's WhatsApp right now.",
     "input_schema": {"type": "object", "properties": {}}},
]


class ChatError(RuntimeError):
    pass


def _config() -> dict:
    return {
        "name": "NBA Agent",
        "model": {"id": MODEL, "effort": EFFORT},
        "system": SYSTEM,
        "tools": [{"type": "custom", **t} for t in CUSTOM_TOOLS],
    }


_client: anthropic.Anthropic | None = None
_lock = threading.Lock()


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ChatError("Chat needs an Anthropic API key: set ANTHROPIC_API_KEY and restart (see README).")
        _client = anthropic.Anthropic()
    return _client


def _state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def _save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=2))


def ensure_agent() -> dict:
    """Create the environment and agent once; update the agent when its config changes."""
    st, c = _state(), client()
    cfg = _config()
    digest_ = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
    if "environment_id" not in st:
        env = c.beta.environments.create(
            name="nba-agent", config={"type": "cloud", "networking": {"type": "limited"}})
        st["environment_id"] = env.id
        _save_state(st)
    if "agent_id" not in st:
        agent = c.beta.agents.create(**cfg)
        st.update(agent_id=agent.id, agent_version=agent.version, config_hash=digest_)
        _save_state(st)
        log.info("created agent %s", agent.id)
    elif st.get("config_hash") != digest_:
        agent = c.beta.agents.update(agent_id=st["agent_id"], **cfg)
        st.update(agent_version=agent.version, config_hash=digest_)
        _save_state(st)
        # A running session stays on the agent version it started with (old tools,
        # old instructions), so start a fresh one. Saved settings carry over.
        store.set_chat_session(1, None)
        log.info("updated agent %s to v%s; next message starts a new conversation", agent.id, agent.version)
    return st


def _new_session(st: dict) -> str:
    session = client().beta.sessions.create(
        agent=st["agent_id"],
        environment_id=st["environment_id"],
        title="NBA chat",
        budget={"type": "limit", "max_list_cost": {"amount": BUDGET_CENTS, "currency": "USD"}},
    )
    store.set_chat_session(1, session.id)
    log.info("new chat session https://platform.claude.com/workspaces/default/sessions/%s", session.id)
    return session.id


def _context_note(channel: str) -> str:
    s = store.get_settings()
    now = datetime.now(timezone.utc).astimezone(ZoneInfo(s.timezone))
    return (f"[channel: {channel}; now: {now:%a %Y-%m-%d %H:%M} {s.timezone} (saved time zone); "
            f"latest game night: {digest.game_night().isoformat()}; "
            f"setup done: {'yes' if s.onboarded else 'NO, run first-time setup'}]")


def _run_tool(name: str, args: dict) -> tuple[str, bool]:
    fn = tools.REGISTRY.get(name)
    if fn is None:
        return f"Unknown tool {name}", True
    try:
        return json.dumps(fn(**(args or {})), ensure_ascii=False), False
    except Exception as e:  # report to the model instead of crashing the turn
        log.exception("tool %s failed", name)
        return f"Tool error: {e}", True


def _answer(session_id: str, event) -> bool:
    out, is_error = _run_tool(event.name, event.input)
    client().beta.sessions.events.send(session_id=session_id, events=[{
        "type": "user.custom_tool_result", "custom_tool_use_id": event.id,
        "content": [{"type": "text", "text": out}]}])
    return event.name in tools.SETTINGS_TOOLS and not is_error


def _settle_pending(session_id: str) -> None:
    """If an earlier reply was cut off mid-tool-call, answer those calls first,
    or the session would wait forever."""
    asked, answered = {}, set()
    for ev in client().beta.sessions.events.list(session_id=session_id, order="asc"):
        if ev.type == "agent.custom_tool_use":
            asked[ev.id] = ev
        elif ev.type == "user.custom_tool_result":
            answered.add(ev.custom_tool_use_id)
    for eid, ev in asked.items():
        if eid not in answered:
            _answer(session_id, ev)


def send(text: str, channel: str = "web") -> dict:
    """One user turn. Returns {reply, settings_changed}."""
    with _lock:  # one turn at a time
        st = ensure_agent()
        session_id = store.get_chat_session(1) or _new_session(st)
        try:
            _settle_pending(session_id)
        except anthropic.NotFoundError:
            session_id = _new_session(st)

        c = client()
        parts, changed, error = [], False, None
        started = time.monotonic()
        with c.beta.sessions.events.stream(session_id=session_id) as stream:  # stream first, then send
            c.beta.sessions.events.send(session_id=session_id, events=[{
                "type": "user.message",
                "content": [{"type": "text", "text": f"{_context_note(channel)}\n{text}"}]}])
            for ev in stream:
                if ev.type == "agent.message":
                    parts += [b.text for b in ev.content if b.type == "text"]
                elif ev.type == "agent.custom_tool_use":
                    changed |= _answer(session_id, ev)
                elif ev.type == "session.error":
                    error = str(getattr(ev, "error", None) or "session error")
                elif ev.type == "session.status_terminated":
                    store.set_chat_session(1, None)
                    break
                elif ev.type == "session.status_idle":
                    reason = ev.stop_reason.type
                    if reason == "requires_action":
                        continue  # waiting on one of our tool results
                    if reason == "budget_reached":
                        store.set_chat_session(1, None)
                        parts.append("(This conversation reached its spending cap, so the next message starts a new chat.)")
                    elif reason == "retries_exhausted":
                        error = error or "the agent failed after retries"
                    break
                if time.monotonic() - started > REPLY_DEADLINE:
                    error = "timed out waiting for the agent"
                    break

        reply = "\n\n".join(p for p in parts if p.strip()) or (f"Sorry, something went wrong: {error}" if error else "…")
        prefix = {"whatsapp": "📱 ", "telegram": "✈️ "}.get(channel, "")
        store.log_chat(1, "user", prefix + text)
        store.log_chat(1, "agent", prefix + reply)
        return {"reply": reply, "settings_changed": changed}


def new_chat() -> None:
    store.set_chat_session(1, None)
    store.clear_chat_log(1)
