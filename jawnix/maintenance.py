from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import BatchArtifact


def expire_batch_files(session: Session, settings: Settings) -> dict[str, int]:
    """Remove only expired CSV files; allocation and artifact metadata are retained."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.batch_retention_days)
    base = Path(settings.batch_dir).resolve()
    removed = missing = skipped = 0
    for artifact in session.scalars(select(BatchArtifact).where(BatchArtifact.created_at < cutoff)):
        path = Path(artifact.path).resolve()
        if not path.is_relative_to(base):
            skipped += 1
            continue
        if path.is_file():
            path.unlink()
            removed += 1
        else:
            missing += 1
    return {"removed": removed, "alreadyMissing": missing, "outsideBatchDir": skipped}
