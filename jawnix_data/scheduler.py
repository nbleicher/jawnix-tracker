from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from jawnix.config import get_settings
from jawnix.database import SessionLocal
from jawnix.maintenance import expire_batch_files

from .scraper import sync_scraper


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jawnix.scheduler")


def seconds_until(hour_utc: int) -> float:
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def run() -> None:
    settings = get_settings()
    while True:
        time.sleep(seconds_until(settings.scraper_hour_utc))
        try:
            with SessionLocal.begin() as session:
                result = sync_scraper(session, settings)
            log.info("Nightly scraper sync: %s", result)
        except Exception:
            log.exception("Nightly scraper sync failed")
        try:
            with SessionLocal.begin() as session:
                result = expire_batch_files(session, settings)
            log.info("Expired batch cleanup: %s", result)
        except Exception:
            log.exception("Expired batch cleanup failed")


if __name__ == "__main__":
    run()
