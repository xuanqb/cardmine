"""进程内定时抓取：按 feed 的间隔判断是否到期。"""

import logging
import threading
from contextlib import closing
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="cardmine-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(20):
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - 定时任务不可中断主进程
                logger.exception("定时抓取失败")
                continue

    def _tick(self) -> None:
        from .api.settings import load_settings
        from .db import connect

        settings = load_settings()
        with closing(connect()) as conn:
            rows = conn.execute(
                "SELECT last_fetched_at, interval_minutes FROM feed WHERE enabled = 1"
            ).fetchall()
        if not rows:
            return

        now = datetime.now(timezone.utc)
        for row in rows:
            last_value = row["last_fetched_at"]
            if not last_value:
                self._run(settings)
                return
            try:
                last = datetime.fromisoformat(last_value)
            except ValueError:
                self._run(settings)
                return
            interval = max(5, int(row["interval_minutes"] or 60)) * 60
            if (now - last).total_seconds() >= interval:
                self._run(settings)
                return

    def _run(self, settings: dict) -> None:
        if self._running.is_set():
            return
        self._running.set()
        try:
            from .collect import run_fetch

            run_fetch(settings)
        finally:
            self._running.clear()


scheduler = Scheduler()
