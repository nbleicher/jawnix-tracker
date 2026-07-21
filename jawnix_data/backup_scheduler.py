from __future__ import annotations

import logging
import os
import subprocess
import time

from .scheduler import seconds_until


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jawnix.backup")


def run() -> None:
    hour = int(os.environ.get("JAWNIX_BACKUP_HOUR_UTC", "4"))
    while True:
        try:
            subprocess.run(["/app/ops/backup.sh"], check=True)
            log.info("Encrypted offsite backup completed")
        except Exception:
            log.exception("Encrypted offsite backup failed")
        time.sleep(seconds_until(hour))


if __name__ == "__main__":
    run()
