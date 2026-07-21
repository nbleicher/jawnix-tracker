from __future__ import annotations

import shlex
import shutil
import subprocess
from html.parser import HTMLParser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import httpx

from sqlalchemy import select
from sqlalchemy.orm import Session

from jawnix.config import Settings
from jawnix.models import ScraperRun

from .migration import import_scraper_sqlite


class _NppesLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def _prepare_nppes(settings: Settings) -> tuple[str, Path] | None:
    """Rotate a stale static NPPES archive before the external scraper runs."""
    response = httpx.get(settings.nppes_index_url, timeout=30, follow_redirects=True)
    response.raise_for_status()
    parser = _NppesLinkParser()
    parser.feed(response.text)
    href = next(
        (
            value
            for value in parser.hrefs
            if "NPPES_Data_Dissemination" in value
            and "V2" in value
            and value.endswith(".zip")
            and "Deactivated" not in value
            and "Weekly" not in value
        ),
        None,
    )
    if href is None:
        raise RuntimeError("Could not determine the current NPPES full-file version.")
    upstream_url = urljoin(settings.nppes_index_url, href)
    data_dir = settings.scraper_db_path.parent
    current = data_dir / "nppes.zip"
    marker = data_dir / "nppes.source-url"
    previous_url = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
    if current.is_file() and previous_url != upstream_url:
        versions = data_dir / "nppes_versions"
        versions.mkdir(parents=True, exist_ok=True)
        previous_name = Path(previous_url).name if previous_url else f"legacy-{current.stat().st_mtime_ns}.zip"
        destination = versions / previous_name
        if destination.exists():
            destination = versions / f"{destination.stem}-{current.stat().st_mtime_ns}.zip"
        shutil.move(current, destination)
    return upstream_url, marker


def sync_scraper(session: Session, settings: Settings, source: str | None = None, force: bool = False) -> dict:
    nppes_version = None
    if settings.scraper_command and source in {None, "nppes"}:
        nppes_version = _prepare_nppes(settings)
    if settings.scraper_command:
        command = shlex.split(settings.scraper_command)
        if source:
            command.extend(["--sources", source])
        subprocess.run(command, check=True)
        if nppes_version and (settings.scraper_db_path.parent / "nppes.zip").is_file():
            upstream_url, marker = nppes_version
            marker.write_text(upstream_url + "\n", encoding="utf-8")
    path = Path(settings.scraper_db_path)
    if not path.is_file():
        raise FileNotFoundError(f"Scraper database was not found: {path}")
    stat = path.stat()
    source_name = source or "health_leads"
    source_version = f"{stat.st_size}:{stat.st_mtime_ns}"
    previous = session.scalar(
        select(ScraperRun)
        .where(ScraperRun.source == source_name, ScraperRun.status == "complete")
        .order_by(ScraperRun.finished_at.desc())
        .limit(1)
    )
    if previous and previous.source_version == source_version and not force:
        return {"skipped": True, "reason": "source version unchanged", "sourceVersion": source_version}
    run = ScraperRun(source=source_name, source_version=source_version, status="running")
    session.add(run)
    session.flush()
    try:
        result = import_scraper_sqlite(session, path)
        run.status = "complete"
        run.checksum = result.get("checksum", "")
        run.rows_seen = result.get("sourceRows", 0)
        run.rows_imported = result.get("imported", 0)
        run.details = result
        if nppes_version:
            run.details = {**result, "nppesUpstream": nppes_version[0]}
        run.finished_at = datetime.now(timezone.utc)
        return result
    except Exception as exc:
        run.status = "failed"
        run.details = {"error": str(exc)}
        run.finished_at = datetime.now(timezone.utc)
        raise
