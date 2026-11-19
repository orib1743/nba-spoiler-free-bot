"""Builds the spoiler-free morning message.

Spoiler rules this module guarantees:
  * never prints a score, a margin, a winner, or the word "overtime";
  * lists recommended games in tip-off order, never "most exciting first";
  * says nothing about games it does not recommend;
  * player lines are "played / did not play" unless the user opted into more.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import analyzer, espn

ET = ZoneInfo("America/New_York")
SKIPPED_STATUSES = {"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_SUSPENDED", "STATUS_FORFEIT"}
REASON_LABEL = {analyzer.CLOSE: "🔥 close finish", analyzer.MATCHUP: "⭐ your matchup"}


def game_night(now: datetime | None = None) -> date:
    """The most recent US game night that has (mostly) been played.

    Before 18:00 US-Eastern that's yesterday's slate (games end ~01:00 ET),
    from 18:00 ET on it's tonight's. So a 08:00 send in Europe
    (01:00-02:00 ET) or a 09:00 send in New York both cover last night."""
    now_et = (now or datetime.now(timezone.utc)).astimezone(ET)
    return now_et.date() - timedelta(days=1) if now_et.hour < 18 else now_et.date()


def analyze_night(night: date, s: dict) -> dict:
    games = [g for g in espn.scoreboard(night) if g["status"] not in SKIPPED_STATUSES]
    final = [g for g in games if g["completed"]]
    pending = [g for g in games if not g["completed"]]

    picks, players = [], []
    summaries = {g["id"]: espn.game_summary(g["id"], final=True) for g in final}
    for g in final:
        reasons = analyzer.evaluate_game(g, summaries[g["id"]]["plays"], s)
        if reasons:
            picks.append({"game": g, "reasons": reasons})

    for p in s.get("players", []):
        line = None
        for g in final:
            line = analyzer.player_status(summaries[g["id"]]["boxscore"], p["id"])
            if line:
                break
        if line is None:
            # Not in any box score: if his team played, he didn't; otherwise no game.
            team_played = any(p.get("team_id") in (g["home"]["id"], g["away"]["id"]) for g in final)
            if not team_played:
                continue
            line = {"played": False, "reason": "", "min": None}
        players.append({"player": p, **line})

    return {"night": night, "games": games, "picks": picks, "players": players, "pending": pending}


def _player_line(entry: dict, detail: str) -> str:
    name = entry["player"]["name"]
    if not entry["played"]:
        why = f" ({entry['reason']})" if detail != "played_only" and entry.get("reason") else ""
        return f"• {name} — did not play{why}"
    if detail == "minutes" and entry.get("min"):
        return f"• {name} — played {entry['min']} min"
    if detail == "stats" and entry.get("min"):
        return (f"• {name} — {entry['min']} min · {entry['pts']} pts · "
                f"{entry['reb']} reb · {entry['ast']} ast")
    return f"• {name} — played"


def render(result: dict, s: dict) -> str:
    night: date = result["night"]
    lines = [f"🏀 NBA picks for {night.strftime('%a, %b %d').replace(' 0', ' ')} — no spoilers", ""]

    if result["picks"]:
        lines.append("Worth watching:")
        for pick in result["picks"]:  # already in tip-off order
            g = pick["game"]
            line = f"• {g['away']['name']} @ {g['home']['name']}"
            if s.get("show_reasons"):
                line += "  " + " · ".join(REASON_LABEL[r] for r in pick["reasons"])
            lines.append(line)
    elif result["games"]:
        lines.append("Nothing on your watch list from last night.")
    else:
        lines.append("No NBA games last night.")

    if result["players"]:
        lines += ["", "Your players:"]
        lines += [_player_line(e, s.get("player_detail", "played_only")) for e in result["players"]]

    if result["pending"]:
        n = len(result["pending"])
        lines += ["", f"⏳ {n} game{'s' if n > 1 else ''} hadn't finished yet and {'were' if n > 1 else 'was'} left out."]
    return "\n".join(lines)


def build(night: date, s: dict) -> tuple[str, dict]:
    result = analyze_night(night, s)
    return render(result, s), result
