from datetime import datetime, timezone

import pytest

from nba_agent import digest, scheduler, senders, store, window


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(scheduler, "_next_try", {})


@pytest.fixture
def world(monkeypatch):
    """Morning message 08:00 UTC; `pending` controls whether games are still running."""
    state = {"pending": [], "sent": []}
    store.save_settings(store.Settings(enabled=True, timezone="UTC", send_time="08:00"))
    monkeypatch.setattr(digest, "build", lambda night, s: ("picks", {"pending": state["pending"]}))
    monkeypatch.setattr(senders, "send", lambda s, text: state["sent"].append(text) or "sent")
    return state


def at(h, m):
    return datetime(2026, 11, 20, h, m, tzinfo=timezone.utc)


def test_waits_for_send_time_inside_window(world):
    assert scheduler.run_once(1, now=at(7, 50), send_by=at(8, 57)) is None
    assert scheduler.run_once(1, now=at(8, 0), send_by=at(8, 57)) == "sent"


def test_sends_right_away_if_window_started_late(world):
    assert scheduler.run_once(1, now=at(8, 20), send_by=at(9, 17)) == "sent"


def test_send_time_after_window_still_sends_before_close(world):
    s = store.get_settings(); s.send_time = "11:00"; store.save_settings(s)
    assert scheduler.run_once(1, now=at(8, 0), send_by=at(8, 57)) is None
    assert scheduler.run_once(1, now=at(8, 57), send_by=at(8, 57)) == "sent"


def test_unfinished_games_dont_block_the_last_chance(world):
    world["pending"] = ["late west coast game"]
    assert scheduler.run_once(1, now=at(8, 0), send_by=at(8, 57)).startswith("waiting")
    assert scheduler.run_once(1, now=at(8, 57), send_by=at(8, 57)) == "sent"


def test_sent_only_once(world):
    scheduler.run_once(1, now=at(8, 0), send_by=at(8, 57))
    assert scheduler.run_once(1, now=at(8, 57), send_by=at(8, 57)) is None
    assert world["sent"] == ["picks"]


def test_seed_settings_only_on_first_run(monkeypatch):
    monkeypatch.setenv("NBA_AGENT_SEED_SETTINGS", store.Settings(send_time="07:30", favorite_teams=["2"]).model_dump_json())
    window.seed_settings_if_empty()
    assert store.get_settings().send_time == "07:30"
    s = store.get_settings(); s.send_time = "09:00"; store.save_settings(s)
    window.seed_settings_if_empty()  # existing settings are never overwritten
    assert store.get_settings().send_time == "09:00"
