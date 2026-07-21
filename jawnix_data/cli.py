from __future__ import annotations

import json
import uuid
from pathlib import Path

import typer
from sqlalchemy import func, select

from jawnix.allocation import allocate_request
from jawnix.config import get_settings
from jawnix.database import SessionLocal
from jawnix.delivery import deliver_request, mark_delivery_failed
from jawnix.maintenance import expire_batch_files
from jawnix.models import Lead, LeadRequest, RequestStatus
from jawnix.states import normalize_states

from .migration import import_agent_config, import_distribution_history, import_manifest, import_scraper_sqlite, import_supabase_jsonl
from .scraper import sync_scraper


app = typer.Typer(no_args_is_help=True, help="Jawnix data migration and batch operations")


def emit(value: object) -> None:
    typer.echo(json.dumps(value, indent=2, default=str))


@app.command("import-config")
def import_config(path: Path):
    with SessionLocal.begin() as session:
        emit(import_agent_config(session, path))


@app.command("import-manifest")
def import_manifest_command(path: Path, expected_sha256: str = ""):
    with SessionLocal.begin() as session:
        emit(import_manifest(session, path, expected_sha256 or None))


@app.command("import-scraper-db")
def import_scraper_command(path: Path, expected_sha256: str = ""):
    with SessionLocal.begin() as session:
        emit(import_scraper_sqlite(session, path, expected_sha256 or None))


@app.command("import-supabase")
def import_supabase_command(directory: Path):
    with SessionLocal.begin() as session:
        emit(import_supabase_jsonl(session, directory))


@app.command("sync-scrapers")
def sync_scrapers(source: str = "", force: bool = False):
    with SessionLocal.begin() as session:
        emit(sync_scraper(session, get_settings(), source or None, force))


@app.command("import-history")
def import_history(directory: Path):
    with SessionLocal.begin() as session:
        emit(import_distribution_history(session, directory))


@app.command("redistribute")
def redistribute(
    request_id: uuid.UUID = typer.Option(..., "--request-id"),
    deliver: bool = True,
):
    settings = get_settings()
    with SessionLocal.begin() as session:
        item = session.get(LeadRequest, request_id)
        if item is None:
            raise typer.BadParameter("request was not found")
        if item.status in {RequestStatus.pending.value, RequestStatus.waiting_inventory.value}:
            item.status = RequestStatus.approved.value
        result = allocate_request(session, request_id, settings)
        emit(result.__dict__)
    if deliver and result.allocated:
        try:
            with SessionLocal.begin() as session:
                emit({"resendMessageId": deliver_request(session, request_id, settings)})
        except Exception as exc:
            with SessionLocal.begin() as session:
                mark_delivery_failed(session, request_id, str(exc))
            raise


@app.command("retry-delivery")
def retry_delivery(request_id: uuid.UUID = typer.Option(..., "--request-id")):
    try:
        with SessionLocal.begin() as session:
            emit({"resendMessageId": deliver_request(session, request_id, get_settings())})
    except Exception as exc:
        with SessionLocal.begin() as session:
            mark_delivery_failed(session, request_id, str(exc))
        raise


@app.command("inventory")
def inventory(states: str = ""):
    selected = normalize_states([value for value in states.split(",") if value]) if states else []
    with SessionLocal() as session:
        query = select(Lead.state, func.count(Lead.id)).group_by(Lead.state).order_by(Lead.state)
        if selected:
            query = query.where(Lead.state.in_(selected))
        emit({state: count for state, count in session.execute(query)})


@app.command("expire-artifacts")
def expire_artifacts():
    with SessionLocal.begin() as session:
        emit(expire_batch_files(session, get_settings()))


if __name__ == "__main__":
    app()
