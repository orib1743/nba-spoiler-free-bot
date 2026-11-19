import re
from datetime import date, datetime, timezone

from nba_agent import analyzer, digest
from nba_agent.store import Settings


def play(period, clock, away, home):
    return {"period": {"number": period}, "clock": {"displayValue": clock}, "awayScore": away, "homeScore": home}


def game(gid, home_id, away_id, home_q, away_q, completed=True):
    return {"id": gid, "start": "2026-11-19T00:00Z", "status": "STATUS_FINAL", "completed": completed,
            "home": {"id": home_id, "abbr": "H", "name": f"Home{home_id}", "score": sum(home_q), "linescores": home_q},
            "away": {"id": away_id, "abbr": "A", "name": f"Away{away_id}", "score": sum(away_q), "linescores": away_q}}


def settings(**kw):
    s = Settings(favorite_teams=["1", "2"]).model_dump()
    s.update(kw)
    return s


# --- analyzer -------------------------------------------------------------

def test_clock_parsing():
    assert analyzer.clock_seconds("11:42") == 702
    assert analyzer.clock_seconds("5:00") == 300
    assert analyzer.clock_seconds("45.3") == 45.3
    assert analyzer.clock_seconds("0.0") == 0


def test_close_window_uses_score_as_window_opens():
    # 4-point game at 5:30, then a run makes it 20 by the end.
    plays = [play(4, "5:30", 90, 94), play(4, "4:10", 90, 104), play(4, "0.0", 90, 110)]
    assert analyzer.closest_margin_in_final_minutes(plays, 5) == 4


def test_close_window_ignores_earlier_quarters():
    plays = [play(3, "2:00", 50, 50), play(4, "6:00", 80, 95), play(4, "3:00", 80, 100)]
    assert analyzer.closest_margin_in_final_minutes(plays, 5) == 15


def test_overtime_counts_as_close():
    plays = [play(4, "8:00", 80, 95), play(4, "0.0", 100, 100), play(5, "2:00", 110, 100)]
    assert analyzer.closest_margin_in_final_minutes(plays, 5) == 0


def test_matchup_rule_needs_both_favorites_and_no_blowout():
    s = settings(close_rules=[])
    tight = game("g", "1", "2", [25, 25, 25, 20], [25, 25, 20, 40])  # 5 apart entering Q4
    blowout = game("g", "1", "2", [40, 40, 30, 20], [20, 20, 20, 20])  # 50 apart
    one_fav = game("g", "1", "9", [25, 25, 25, 20], [25, 25, 20, 40])
    assert analyzer.evaluate_game(tight, [], s) == [analyzer.MATCHUP]
    assert analyzer.evaluate_game(blowout, [], s) == []
    assert analyzer.evaluate_game(one_fav, [], s) == []


def test_matchup_margin_is_strictly_less_than():
    s = settings(close_rules=[])
    g15 = game("g", "1", "2", [40, 30, 30, 0], [30, 25, 30, 0])  # exactly 15 apart
    assert analyzer.evaluate_game(g15, [], s) == []


def test_close_rule_scope():
    plays = [play(4, "3:00", 100, 101)]
    g = game("g", "8", "9", [1, 1, 1, 1], [1, 1, 1, 1])
    assert analyzer.evaluate_game(g, plays, settings()) == [analyzer.CLOSE]
    s = settings(close_rules=[{"enabled": True, "minutes_left": 5, "max_margin": 5, "scope": "favorites"}])
    assert analyzer.evaluate_game(g, plays, s) == []


def test_two_close_rules_favorites_loose_everyone_strict():
    """The user's setup: my teams within 5 in the last 5 min, OR any game within 3 in the last 2 min."""
    rules = [{"enabled": True, "minutes_left": 5, "max_margin": 5, "scope": "favorites"},
             {"enabled": True, "minutes_left": 2, "max_margin": 3, "scope": "all"}]
    s = settings(close_rules=rules, matchup_rule={"enabled": False})
    within5_at_4min = [play(4, "4:00", 100, 104), play(4, "0.0", 100, 112)]
    within2_at_1min = [play(4, "4:00", 90, 100), play(4, "1:00", 100, 102), play(4, "0.0", 100, 104)]
    other = game("g", "8", "9", [1, 1, 1, 1], [1, 1, 1, 1])
    fav = game("g", "1", "9", [1, 1, 1, 1], [1, 1, 1, 1])
    assert analyzer.evaluate_game(fav, within5_at_4min, s) == [analyzer.CLOSE]      # rule 1
    assert analyzer.evaluate_game(other, within5_at_4min, s) == []                  # rule 1 is favorites-only
    assert analyzer.evaluate_game(other, within2_at_1min, s) == [analyzer.CLOSE]    # rule 2
    assert analyzer.evaluate_game(fav, within2_at_1min, s) == [analyzer.CLOSE]      # counted once


def test_old_single_close_rule_settings_still_load():
    old = {"close_rule": {"enabled": True, "minutes_left": 4, "max_margin": 6, "scope": "favorites"}}
    s = Settings.model_validate(old)
    assert [(r.minutes_left, r.max_margin, r.scope) for r in s.close_rules] == [(4, 6, "favorites")]
    assert len(Settings().close_rules) == 1  # new users get the default rule


def test_player_status():
    box = {"players": [{"team": {"id": "5"}, "statistics": [{
        "labels": ["MIN", "PTS", "REB", "AST"],
        "athletes": [
            {"athlete": {"id": "11"}, "didNotPlay": False, "reason": "COACH'S DECISION", "stats": ["33", "20", "3", "5"]},
            {"athlete": {"id": "12"}, "didNotPlay": True, "reason": "INJURY/ILLNESS", "stats": []},
        ]}]}]}
    p = analyzer.player_status(box, "11")
    assert p["played"] and p["min"] == "33" and p["reason"] == ""
    assert analyzer.player_status(box, "12") == {"played": False, "reason": "Injury/Illness", "team_id": "5",
                                                  "min": None, "pts": None, "reb": None, "ast": None}
    assert analyzer.player_status(box, "99") is None


# --- digest ---------------------------------------------------------------

def test_game_night():
    # 08:00 in Athens = 01:00 New York -> previous night
    assert digest.game_night(datetime(2026, 11, 19, 6, 0, tzinfo=timezone.utc)) == date(2026, 11, 18)
    # 09:00 New York -> previous night
    assert digest.game_night(datetime(2026, 11, 19, 14, 0, tzinfo=timezone.utc)) == date(2026, 11, 18)
    # 20:00 New York -> tonight
    assert digest.game_night(datetime(2026, 11, 20, 1, 0, tzinfo=timezone.utc)) == date(2026, 11, 19)


def test_message_never_contains_scores():
    g = game("g", "1", "2", [31, 29, 26, 23], [31, 36, 25, 30])
    result = {"night": date(2026, 11, 18), "games": [g],
              "picks": [{"game": g, "reasons": [analyzer.CLOSE, analyzer.MATCHUP]}],
              "players": [{"player": {"name": "Some Player"}, "played": True, "min": "34", "pts": "41",
                           "reb": "9", "ast": "7", "reason": ""}],
              "pending": []}
    for show_reasons in (False, True):
        for detail in ("played_only", "minutes"):
            text = digest.render(result, settings(show_reasons=show_reasons, player_detail=detail))
            body = text.split("\n", 1)[1]  # skip the dated header
            for score in ("109", "122", "41", "beat", "won", "lost", "overtime", "OT"):
                assert not re.search(rf"\b{score}\b", body), (score, text)
    assert "41 pts" in digest.render(result, settings(player_detail="stats"))  # only when opted in


def test_reasons_hidden_by_default():
    g = game("g", "1", "2", [1, 1, 1, 1], [1, 1, 1, 1])
    result = {"night": date(2026, 11, 18), "games": [g], "picks": [{"game": g, "reasons": [analyzer.CLOSE]}],
              "players": [], "pending": [g]}
    text = digest.render(result, settings())
    assert "close finish" not in text and "Away2 @ Home1" in text and "1 game hadn't finished" in text
