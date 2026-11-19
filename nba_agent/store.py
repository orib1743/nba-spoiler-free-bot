"""Settings + send history in SQLite. Multi-user shaped (user_id everywhere) so
the app can be opened to other people later; today the web UI edits user 1."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from .paths import DATA_DIR
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

DB_PATH = DATA_DIR / "nba_agent.db"


class MatchupRule(BaseModel):
    enabled: bool = True
    max_margin_entering_q4: int = Field(15, ge=1, le=60)


class CloseRule(BaseModel):
    enabled: bool = True
    minutes_left: float = Field(5, ge=1, le=12)
    max_margin: int = Field(5, ge=0, le=30)
    scope: Literal["all", "favorites"] = "all"


class Player(BaseModel):
    id: str
    name: str
    team_id: str = ""


class Sender(BaseModel):
    # Where the morning message goes. meta = official WhatsApp Cloud API.
    type: Literal["console", "telegram", "meta", "callmebot"] = "console"
    callmebot_apikey: str = ""


class Settings(BaseModel):
    onboarded: bool = False               # set by the agent once the first-chat setup is done
    enabled: bool = False
    phone: str = ""                       # WhatsApp, international format, e.g. +15551234567
    telegram_chat_id: str = ""            # set automatically when you first message the Telegram bot
    timezone: str = "UTC"
    send_time: str = "08:00"              # HH:MM in `timezone`
    wait_for_final_hours: float = Field(3, ge=0, le=12)
    favorite_teams: list[str] = []        # ESPN team ids
    matchup_rule: MatchupRule = MatchupRule()
    close_rules: list[CloseRule] = Field(default_factory=lambda: [CloseRule()])  # a game counts if ANY rule matches
    players: list[Player] = []
    player_detail: Literal["played_only", "minutes", "stats"] = "played_only"
    show_reasons: bool = False
    sender: Sender = Sender()

    @model_validator(mode="before")
    @classmethod
    def _migrate_single_close_rule(cls, data):
        """Settings saved before multiple close rules had one `close_rule` object."""
        if isinstance(data, dict) and "close_rule" in data:
            data = dict(data)
            old = data.pop("close_rule")
            data.setdefault("close_rules", [old] if old else [])
        return data

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown time zone {v!r}")
        return v

    @field_validator("send_time")
    @classmethod
    def _time(cls, v: str) -> str:
        h, m = v.split(":")
        if not (0 <= int(h) < 24 and 0 <= int(m) < 60):
            raise ValueError("send_time must be HH:MM")
        return f"{int(h):02d}:{int(m):02d}"


@contextmanager
def _conn():
    """Connection that commits on success and is always closed."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    try:
        with c:
            yield _init(c)
    finally:
        c.close()


def _init(c: sqlite3.Connection) -> sqlite3.Connection:
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, settings TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS wa_inbound (
            user_id INTEGER PRIMARY KEY, last_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS wa_seen (
            msg_id TEXT PRIMARY KEY, at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS chat_state (
            user_id INTEGER PRIMARY KEY, session_id TEXT);
        CREATE TABLE IF NOT EXISTS chat_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            role TEXT NOT NULL, text TEXT NOT NULL, at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sends (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            night TEXT NOT NULL, sent_at TEXT NOT NULL, kind TEXT NOT NULL,
            ok INTEGER NOT NULL, text TEXT NOT NULL, error TEXT);
    """)
    return c


def get_settings(user_id: int = 1) -> Settings:
    with _conn() as c:
        row = c.execute("SELECT settings FROM users WHERE id=?", (user_id,)).fetchone()
    return Settings.model_validate_json(row["settings"]) if row else Settings()


def save_settings(s: Settings, user_id: int = 1) -> None:
    with _conn() as c:
        c.execute("INSERT INTO users(id, settings) VALUES(?, ?) "
                  "ON CONFLICT(id) DO UPDATE SET settings=excluded.settings",
                  (user_id, s.model_dump_json()))


def user_ids() -> list[int]:
    with _conn() as c:
        return [r["id"] for r in c.execute("SELECT id FROM users")]


def log_send(user_id: int, night: str, kind: str, ok: bool, text: str, error: str | None = None) -> None:
    with _conn() as c:
        c.execute("INSERT INTO sends(user_id, night, sent_at, kind, ok, text, error) VALUES(?,?,?,?,?,?,?)",
                  (user_id, night, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   kind, int(ok), text, error))


def already_sent(user_id: int, night: str) -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM sends WHERE user_id=? AND night=? AND kind='daily' AND ok=1",
                         (user_id, night)).fetchone() is not None


def history(user_id: int = 1, limit: int = 20) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT night, sent_at, kind, ok, text, error FROM sends "
                         "WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))
        return [dict(r) for r in rows]


def get_chat_session(user_id: int) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT session_id FROM chat_state WHERE user_id=?", (user_id,)).fetchone()
    return row["session_id"] if row else None


def set_chat_session(user_id: int, session_id: str | None) -> None:
    with _conn() as c:
        c.execute("INSERT INTO chat_state(user_id, session_id) VALUES(?, ?) "
                  "ON CONFLICT(user_id) DO UPDATE SET session_id=excluded.session_id", (user_id, session_id))


def log_chat(user_id: int, role: str, text: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO chat_log(user_id, role, text, at) VALUES(?,?,?,?)",
                  (user_id, role, text, datetime.now(timezone.utc).isoformat(timespec="seconds")))


def chat_log(user_id: int = 1, limit: int = 100) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT role, text, at FROM chat_log WHERE user_id=? ORDER BY id DESC LIMIT ?",
                         (user_id, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def clear_chat_log(user_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM chat_log WHERE user_id=?", (user_id,))


def get_kv(key: str) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_kv(key: str, value: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))


def mark_inbound(user_id: int) -> None:
    with _conn() as c:
        c.execute("INSERT INTO wa_inbound(user_id, last_at) VALUES(?, ?) "
                  "ON CONFLICT(user_id) DO UPDATE SET last_at=excluded.last_at",
                  (user_id, datetime.now(timezone.utc).isoformat(timespec="seconds")))


def get_last_inbound(user_id: int) -> datetime | None:
    with _conn() as c:
        row = c.execute("SELECT last_at FROM wa_inbound WHERE user_id=?", (user_id,)).fetchone()
    return datetime.fromisoformat(row["last_at"]) if row else None


def first_time_seen(msg_id: str) -> bool:
    """True the first time a WhatsApp message id is seen (Meta may deliver a webhook twice)."""
    with _conn() as c:
        cur = c.execute("INSERT OR IGNORE INTO wa_seen(msg_id, at) VALUES(?, ?)",
                        (msg_id, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        return cur.rowcount == 1


def as_dict(s: Settings) -> dict:
    return json.loads(s.model_dump_json())
