"""Decides which finished games are worth watching. Pure functions, no I/O."""
from __future__ import annotations

CLOSE = "close"        # tight in the final minutes (or overtime)
MATCHUP = "matchup"    # two of your teams, still competitive entering the 4th


def clock_seconds(display: str) -> float:
    """ESPN clock strings: '11:42', '5:00', '45.3', '0.0'."""
    if ":" in display:
        m, s = display.split(":", 1)
        return int(m) * 60 + float(s)
    return float(display)


def margin_entering_q4(game: dict) -> int | None:
    home, away = game["home"]["linescores"], game["away"]["linescores"]
    if len(home) < 3 or len(away) < 3:
        return None
    return abs(sum(home[:3]) - sum(away[:3]))


def closest_margin_in_final_minutes(plays: list[dict], minutes: float) -> int | None:
    """Smallest score margin at any moment in the last `minutes` of the 4th
    quarter or at any point in overtime. None if that window never started."""
    window = minutes * 60
    home = away = 0
    best: int | None = None
    entered = False
    for p in plays:
        period = (p.get("period") or {}).get("number", 0)
        try:
            secs = clock_seconds((p.get("clock") or {}).get("displayValue", "12:00"))
        except ValueError:
            continue
        in_window = period >= 5 or (period == 4 and secs <= window)
        if in_window and not entered:
            entered = True
            best = abs(home - away)  # the score as the window opened
        home = p.get("homeScore", home)
        away = p.get("awayScore", away)
        if in_window:
            m = abs(home - away)
            best = m if best is None else min(best, m)
    return best


def evaluate_game(game: dict, plays: list[dict] | None, s: dict) -> list[str]:
    """Reasons this game is recommended under settings `s` (empty = skip it)."""
    reasons: list[str] = []
    favs = set(s.get("favorite_teams", []))
    ids = {game["home"]["id"], game["away"]["id"]}
    has_fav = bool(ids & favs)

    for close in s.get("close_rules", []):  # any one matching rule is enough
        if close.get("enabled") and plays is not None and (close.get("scope") == "all" or has_fav):
            m = closest_margin_in_final_minutes(plays, float(close.get("minutes_left", 5)))
            if m is not None and m <= int(close.get("max_margin", 5)):
                reasons.append(CLOSE)
                break

    match = s.get("matchup_rule", {})
    if match.get("enabled") and ids <= favs:
        m = margin_entering_q4(game)
        if m is not None and m < int(match.get("max_margin_entering_q4", 15)):
            reasons.append(MATCHUP)
    return reasons


def player_status(boxscore: dict, player_id: str) -> dict | None:
    """The player's line in this box score, or None if he isn't in it."""
    for team in boxscore.get("players", []):
        for group in team.get("statistics", []):
            labels = group.get("labels", [])
            for a in group.get("athletes", []):
                if str(a.get("athlete", {}).get("id")) != str(player_id):
                    continue
                stats = dict(zip(labels, a.get("stats", [])))
                played = not a.get("didNotPlay") and stats.get("MIN") not in (None, "", "0", "--")
                return {
                    "played": played,
                    "reason": (a.get("reason") or "").title() if not played else "",
                    "team_id": str(team.get("team", {}).get("id", "")),
                    "min": stats.get("MIN"),
                    "pts": stats.get("PTS"),
                    "reb": stats.get("REB"),
                    "ast": stats.get("AST"),
                }
    return None
