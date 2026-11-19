"""The tools the chat agent can call. Each returns a JSON-able dict.

Spoiler design: the everyday tool (get_games) never returns scores, so the
agent can't leak what it never saw. Results live in separate tools
(get_game_result, get_player_line, get_standings) that the system prompt only
allows after the user explicitly asks.
"""
from __future__ import annotations

import difflib
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import analyzer, digest, espn, senders, store


# ---------- helpers ----------

def _teams_by_abbr() -> dict[str, dict]:
    return {t["abbr"]: t for t in espn.teams()}


def _team_id(abbr_or_name: str) -> str | None:
    q = abbr_or_name.strip().lower()
    for t in espn.teams():
        if q in (t["abbr"].lower(), t["name"].lower(), t["short"].lower(), t["id"]):
            return t["id"]
    return None


def _local(iso: str, tz: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt.astimezone(ZoneInfo(tz)).strftime("%a %b %d, %H:%M")


def _night(d: str | None) -> date:
    return date.fromisoformat(d) if d else digest.game_night()


def find_players(name: str) -> list[dict]:
    """Fuzzy search across all 30 current rosters."""
    q = name.strip().lower()
    everyone = [{**p, "team_id": t["id"], "team": t["abbr"]} for t in espn.teams() for p in espn.roster(t["id"])]
    exact = [p for p in everyone if q in p["name"].lower()]
    if exact:
        return exact[:5]
    names = [p["name"].lower() for p in everyone]
    close = set(difflib.get_close_matches(q, names, n=5, cutoff=0.6))
    return [p for p in everyone if p["name"].lower() in close]


# ---------- settings ----------

def get_settings() -> dict:
    s = store.get_settings()
    by_id = {t["id"]: t for t in espn.teams()}
    return {
        "setup_done": s.onboarded,
        "daily_message_enabled": s.enabled,
        "send_time": s.send_time,
        "timezone": s.timezone,
        "wait_for_late_games_hours": s.wait_for_final_hours,
        "favorite_teams": sorted(f"{by_id[i]['name']} ({by_id[i]['abbr']})" for i in s.favorite_teams if i in by_id),
        "matchup_rule": s.matchup_rule.model_dump(),
        "close_finish_rules": [r.model_dump() for r in s.close_rules],
        "players": [p.name for p in s.players],
        "player_detail_in_morning_message": s.player_detail,
        "show_reasons_in_morning_message": s.show_reasons,
        "morning_message_delivered_via": {"telegram": "telegram", "meta": "whatsapp", "callmebot": "whatsapp",
                                          "console": "nowhere yet (dry run)"}[s.sender.type],
        "connected_channels": [name for name, ok in (("telegram", bool(s.telegram_chat_id)),
                                                     ("whatsapp", bool(s.phone))) if ok],
    }


def update_settings(**c) -> dict:
    s = store.get_settings()
    notes = []
    simple = {"onboarded": "onboarded", "enabled": "enabled", "send_time": "send_time", "timezone": "timezone",
              "wait_for_final_hours": "wait_for_final_hours", "show_reasons": "show_reasons",
              "player_detail": "player_detail"}
    data = s.model_dump()
    for key, field in simple.items():
        if c.get(key) is not None:
            data[field] = c[key]
    for key, (rule, field) in {
        "matchup_enabled": ("matchup_rule", "enabled"),
        "matchup_max_margin_entering_q4": ("matchup_rule", "max_margin_entering_q4"),
    }.items():
        if c.get(key) is not None:
            data[rule][field] = c[key]
    if c.get("close_rules") is not None:  # the complete new list replaces the old one
        data["close_rules"] = [{"enabled": True, **r} for r in c["close_rules"]]

    favs = list(data["favorite_teams"])
    for t in c.get("add_teams") or []:
        tid = _team_id(t)
        if tid is None:
            notes.append(f"Unknown team: {t}")
        elif tid not in favs:
            favs.append(tid)
    for t in c.get("remove_teams") or []:
        tid = _team_id(t)
        if tid in favs:
            favs.remove(tid)
    data["favorite_teams"] = favs

    players = list(data["players"])
    for name in c.get("add_players") or []:
        matches = find_players(name)
        if len(matches) == 1 or (matches and matches[0]["name"].lower() == name.strip().lower()):
            m = matches[0]
            if all(p["id"] != m["id"] for p in players):
                players.append({"id": m["id"], "name": m["name"], "team_id": m["team_id"]})
        elif matches:
            notes.append(f"'{name}' is ambiguous, not added. Ask which one: " +
                         ", ".join(f"{m['name']} ({m['team']})" for m in matches))
        else:
            notes.append(f"No current NBA player found matching '{name}'.")
    for name in c.get("remove_players") or []:
        q = name.strip().lower()
        players = [p for p in players if q not in p["name"].lower()]
    data["players"] = players

    channel = c.get("delivery_channel")
    if channel == "telegram":
        if s.telegram_chat_id:
            data["sender"]["type"] = "telegram"
        else:
            notes.append("Telegram isn't connected: the user has to message the Telegram bot first.")
    elif channel == "whatsapp":
        if s.phone:
            data["sender"]["type"] = "meta" if s.sender.type != "callmebot" else "callmebot"
        else:
            notes.append("WhatsApp isn't connected: the user has to message the WhatsApp bot first.")

    try:
        new = store.Settings.model_validate(data)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    store.save_settings(new)
    return {"ok": True, "notes": notes, "settings_now": get_settings()}


# ---------- games (spoiler-free) ----------

def get_games(night: str | None = None, team: str | None = None) -> dict:
    """Games on a US game night with status and your rules' verdict. NO scores."""
    s = store.get_settings()
    d = _night(night)
    tid = _team_id(team) if team else None
    favs = set(s.favorite_teams)
    out = []
    for g in espn.scoreboard(d):
        if tid and tid not in (g["home"]["id"], g["away"]["id"]):
            continue
        item = {
            "game_id": g["id"],
            "matchup": f"{g['away']['name']} @ {g['home']['name']}",
            "tip_off_local": _local(g["start"], s.timezone),
            "involves_favorite_team": bool({g["home"]["id"], g["away"]["id"]} & favs),
        }
        if g["completed"]:
            plays = espn.game_summary(g["id"], final=True)["plays"]
            reasons = analyzer.evaluate_game(g, plays, digest_settings(s))
            item["status"] = "final"
            item["worth_watching_by_your_rules"] = bool(reasons)
            item["why"] = [{"close": "close finish", "matchup": "two favorite teams, competitive"}[r] for r in reasons]
        elif g["status"] in digest.SKIPPED_STATUSES:
            item["status"] = "postponed/canceled"
        elif g["status"] == "STATUS_SCHEDULED":
            item["status"] = "not started"
        else:
            item["status"] = "in progress"
        out.append(item)
    return {"night": d.isoformat(), "games": out}


def digest_settings(s: store.Settings) -> dict:
    return store.as_dict(s)


def get_schedule(team: str, days: int = 7) -> dict:
    """Upcoming games for a team (future games carry no spoilers)."""
    tid = _team_id(team)
    if tid is None:
        return {"error": f"Unknown team {team!r}"}
    s = store.get_settings()
    now = datetime.now(timezone.utc)
    start = now.astimezone(digest.ET).date()
    out = []
    for i in range(min(max(days, 1), 14)):
        for g in espn.scoreboard(start + timedelta(days=i)):
            if tid in (g["home"]["id"], g["away"]["id"]) and not g["completed"]:
                out.append({"matchup": f"{g['away']['name']} @ {g['home']['name']}",
                            "tip_off_local": _local(g["start"], s.timezone),
                            "status": "in progress" if g["status"] == "STATUS_IN_PROGRESS" else "upcoming"})
    return {"team": team, "games": out}


# ---------- spoiler tools ----------

def get_game_result(game_id: str) -> dict:
    """SPOILER: final score, quarter-by-quarter, and each team's top scorer."""
    for back in range(0, 4):
        for g in espn.scoreboard(digest.game_night() - timedelta(days=back)):
            if g["id"] == game_id:
                break
        else:
            continue
        break
    else:
        return {"error": "Game not found in the last few nights."}
    if not g["completed"]:
        return {"error": "That game hasn't finished yet."}
    box = espn.game_summary(g["id"], final=True)["boxscore"]
    top = {}
    for team in box.get("players", []):
        grp = team["statistics"][0]
        i = grp["labels"].index("PTS")
        best = max((a for a in grp["athletes"] if a.get("stats")), key=lambda a: int(a["stats"][i] or 0), default=None)
        if best:
            top[team["team"]["abbreviation"]] = f"{best['athlete']['displayName']} {best['stats'][i]} pts"
    return {"matchup": f"{g['away']['name']} @ {g['home']['name']}",
            "final": f"{g['away']['abbr']} {g['away']['score']} - {g['home']['abbr']} {g['home']['score']}",
            "by_quarter": {g["away"]["abbr"]: g["away"]["linescores"], g["home"]["abbr"]: g["home"]["linescores"]},
            "top_scorers": top}


def get_player_line(player: str, night: str | None = None, detail: str = "played_only") -> dict:
    """Did a player play on a night; full box line only with detail='full' (SPOILER)."""
    matches = find_players(player)
    if not matches:
        return {"error": f"No current NBA player matching {player!r}"}
    if len(matches) > 1 and matches[0]["name"].lower() != player.strip().lower():
        return {"ambiguous": [f"{m['name']} ({m['team']})" for m in matches]}
    p = matches[0]
    d = _night(night)
    for g in espn.scoreboard(d):
        if p["team_id"] not in (g["home"]["id"], g["away"]["id"]):
            continue
        if not g["completed"]:
            return {"player": p["name"], "night": d.isoformat(), "status": "game not finished yet"}
        line = analyzer.player_status(espn.game_summary(g["id"], final=True)["boxscore"], p["id"])
        played = bool(line and line["played"])
        out = {"player": p["name"], "night": d.isoformat(), "played": played}
        if detail == "full" and line:
            out.update({k: line[k] for k in ("min", "pts", "reb", "ast", "reason") if line.get(k)})
        return out
    return {"player": p["name"], "night": d.isoformat(), "status": "his team had no game"}


def get_standings() -> dict:
    """SPOILER-ish: standings include last night's results."""
    data = espn._get("https://site.api.espn.com/apis/v2/sports/basketball/nba/standings")
    out = {}
    for conf in data.get("children", []):
        rows = []
        for e in conf["standings"]["entries"]:
            st = {x["name"]: x.get("displayValue") for x in e["stats"]}
            rows.append((float(st.get("playoffSeed") or 99),
                         f"{e['team']['abbreviation']} {st.get('overall')} (streak {st.get('streak')}, GB {st.get('gamesBehind')})"))
        out[conf["name"]] = [r for _, r in sorted(rows)]
    return out


# ---------- morning message ----------

def preview_morning_message(night: str | None = None) -> dict:
    s = store.get_settings()
    d = _night(night)
    text, _ = digest.build(d, store.as_dict(s))
    return {"night": d.isoformat(), "message": text}


def send_morning_message_now() -> dict:
    s = store.get_settings()
    d = digest.game_night()
    text, _ = digest.build(d, store.as_dict(s))
    try:
        info = senders.send(s, text)
    except senders.SendError as e:
        return {"ok": False, "error": str(e)}
    store.log_send(1, d.isoformat(), "chat", True, text)
    return {"ok": True, "info": info}


REGISTRY = {
    "get_settings": get_settings,
    "update_settings": update_settings,
    "get_games": get_games,
    "get_schedule": get_schedule,
    "get_game_result": get_game_result,
    "get_player_line": get_player_line,
    "get_standings": get_standings,
    "preview_morning_message": preview_morning_message,
    "send_morning_message_now": send_morning_message_now,
}
SETTINGS_TOOLS = {"update_settings"}
