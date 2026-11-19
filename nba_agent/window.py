"""Run the bot for a limited window, then exit. Made for a free daily GitHub
Actions run (see .github/workflows/daily.yml):

  python run.py window 70

For `minutes` minutes it answers Telegram messages (including ones sent while
it was off; Telegram keeps them for 24 hours) and sends the morning message:
at the send time if that falls inside the window, right away if it has already
passed, and in any case before the window closes.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from . import scheduler, store, telegram

log = logging.getLogger("nba_agent.window")
SEND_MARGIN = timedelta(minutes=3)  # send at the latest this long before the window closes


def seed_settings_if_empty() -> None:
    """First run on a fresh server: load settings from NBA_AGENT_SEED_SETTINGS (JSON)."""
    seed = os.environ.get("NBA_AGENT_SEED_SETTINGS", "").strip()
    if seed and 1 not in store.user_ids():
        store.save_settings(store.Settings.model_validate_json(seed))
        log.info("loaded settings from NBA_AGENT_SEED_SETTINGS")


def run(minutes: float) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    for noisy in ("httpx", "httpx2", "httpcore"):  # their URLs contain the bot token
        logging.getLogger(noisy).setLevel(logging.WARNING)
    seed_settings_if_empty()

    start = datetime.now(timezone.utc)
    end = start + timedelta(minutes=minutes)
    send_by = end - SEND_MARGIN
    log.info("window open until %s UTC", end.strftime("%H:%M"))

    stop = threading.Event()
    poller = None
    if telegram.configured():
        poller = threading.Thread(target=telegram.poll_forever, args=(stop,), name="telegram")
        poller.start()
    else:
        log.warning("TELEGRAM_BOT_TOKEN not set: only the morning message will run")

    while datetime.now(timezone.utc) < end:
        for uid in store.user_ids():
            try:
                status = scheduler.run_once(uid, send_by=send_by)
                if status:
                    log.info("morning message: %s", status)
            except Exception:
                log.exception("scheduler error")
        time.sleep(min(60, max(1, (end - datetime.now(timezone.utc)).total_seconds())))

    log.info("window closing: finishing replies in progress")
    stop.set()
    if poller:
        poller.join(timeout=70)  # a long poll can take up to ~50 s to return
    telegram.drain()
    log.info("window closed")
