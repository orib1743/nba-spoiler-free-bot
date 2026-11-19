"""Thin client for ESPN's public (unauthenticated) NBA JSON endpoints.

Only finished-game summaries are cached on disk: once a game is final its data
never changes, so we never fetch it twice.
"""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

import httpx

from .paths import DATA_DIR

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
CACHE_DIR = DATA_DIR / "cache"

_client = httpx.Client(timeout=20.0, headers={"User-Agent": "nba-agent/1.0"})
_memo: dict[str, tuple[float, object]] = {}


def _get(url: str, params: dict | None = None) -> dict:
    resp = _client.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


def _memoized(key: str, ttl: float, fn):
    hit = _memo.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = fn()
    _memo[key] = (time.time(), value)
    return value


def scoreboard(day: date) -> list[dict]:
    """All games on a US-Eastern calendar date, simplified."""
    data = _get(f"{BASE}/scoreboard", {"dates": day.strftime("%Y%m%d")})
    games = []
    for ev in data.get("events", []):
        comp = ev["competitions"][0]
        teams = {}
        for c in comp["competitors"]:
            teams[c["homeAway"]] = {
                "id": c["team"]["id"],
                "abbr": c["team"]["abbreviation"],
                "name": c["team"]["shortDisplayName"],
                "score": int(c.get("score") or 0),
                "linescores": [int(float(q.get("value", 0))) for q in c.get("linescores", [])],
            }
        games.append({
            "id": ev["id"],
            "start": ev["date"],  # ISO UTC tip-off time
            "status": ev["status"]["type"]["name"],  # STATUS_FINAL, STATUS_IN_PROGRESS, ...
            "completed": bool(ev["status"]["type"].get("completed")),
            "home": teams["home"],
            "away": teams["away"],
        })
    games.sort(key=lambda g: g["start"])
    return games


def game_summary(event_id: str, final: bool) -> dict:
    """Play-by-play + box score for one game. Cached on disk when final."""
    path = CACHE_DIR / f"{event_id}.json"
    if final and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    data = _get(f"{BASE}/summary", {"event": event_id})
    slim = {"plays": data.get("plays", []), "boxscore": data.get("boxscore", {})}
    if final:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(slim), encoding="utf-8")
    return slim


def teams() -> list[dict]:
    def load():
        data = _get(f"{BASE}/teams")
        out = []
        for t in data["sports"][0]["leagues"][0]["teams"]:
            t = t["team"]
            out.append({
                "id": t["id"],
                "abbr": t["abbreviation"],
                "name": t["displayName"],
                "short": t["shortDisplayName"],
                "color": t.get("color", "888888"),
                "logo": (t.get("logos") or [{}])[0].get("href", ""),
            })
        return sorted(out, key=lambda t: t["name"])
    return _memoized("teams", 12 * 3600, load)


def roster(team_id: str) -> list[dict]:
    def load():
        data = _get(f"{BASE}/teams/{team_id}/roster")
        return sorted(
            ({"id": a["id"], "name": a["displayName"],
              "pos": (a.get("position") or {}).get("abbreviation", "")}
             for a in data.get("athletes", [])),
            key=lambda a: a["name"],
        )
    return _memoized(f"roster:{team_id}", 12 * 3600, load)
