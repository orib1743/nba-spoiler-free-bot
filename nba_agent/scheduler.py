"""Background loop: at each user's send time, build and send the digest.

If some games from last night are still being played at send time, it waits
(re-checking every 10 minutes) up to `wait_for_final_hours`, then sends what it
has with a "N games hadn't finished" note."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import digest, senders, store

log = logging.getLogger("nba_agent.scheduler")
RETRY_SECONDS = 600
_next_try: dict[int, float] = {}


def run_once(user_id: int, now: datetime | None = None, send_by: datetime | None = None) -> str | None:
    """Send today's digest for one user if it's due. Returns a status string.

    `send_by` is a hard deadline (used when the app only runs for a short daily
    window, e.g. on GitHub Actions): from then on the message goes out even if
    the send time hasn't come yet or games are still being played."""
    now = now or datetime.now(timezone.utc)
    s = store.get_settings(user_id)
    if not s.enabled:
        return None
    forced = send_by is not None and now >= send_by
    local = now.astimezone(ZoneInfo(s.timezone))
    h, m = map(int, s.send_time.split(":"))
    due_at = local.replace(hour=h, minute=m, second=0, microsecond=0)
    if local < due_at and not forced:
        return None
    night = digest.game_night(now)
    if store.already_sent(user_id, night.isoformat()):
        return None
    if time.time() < _next_try.get(user_id, 0) and not forced:
        return None

    text, result = digest.build(night, store.as_dict(s))
    deadline = due_at + timedelta(hours=s.wait_for_final_hours)
    if result["pending"] and local < deadline and not forced:
        retry = time.time() + RETRY_SECONDS
        _next_try[user_id] = min(retry, send_by.timestamp()) if send_by else retry
        return f"waiting: {len(result['pending'])} game(s) still in progress"

    try:
        info = senders.send(s, text)
        store.log_send(user_id, night.isoformat(), "daily", True, text)
        return info
    except Exception as e:  # keep the loop alive; retry later
        store.log_send(user_id, night.isoformat(), "daily", False, text, str(e))
        _next_try[user_id] = time.time() + RETRY_SECONDS
        log.exception("send failed for user %s", user_id)
        return f"failed: {e}"


async def loop() -> None:
    while True:
        for uid in store.user_ids():
            try:
                status = await asyncio.to_thread(run_once, uid)
                if status:
                    log.info("user %s: %s", uid, status)
            except Exception:
                log.exception("scheduler error for user %s", uid)
                _next_try[uid] = time.time() + RETRY_SECONDS
        await asyncio.sleep(60)
