# Jawnix VPS batch platform

Jawnix is a customer batch-request portal backed by FastAPI and private PostgreSQL. Supabase remains the identity provider; application data lives on the VPS. Slack handles approval and Resend delivers an exact `phone,title` CSV.

Billing, invoice, Stripe, and finance code remains in the repository for rollback, but the VPS application does not serve those routes and the new UI does not expose them. Keep `JAWNIX_ENABLE_BILLING=false`.

## Services

- Caddy: public HTTP/HTTPS and static portal
- FastAPI: session, profile, request, admin, Slack, and Resend APIs
- Worker: durable approval, allocation, Slack, and delivery jobs
- PostgreSQL 18: private inventory and permanent allocation history
- Scheduler: nightly scraper sync and 30-day CSV cleanup
- Backup worker: encrypted Restic dump and WAL backup with 14-day retention

Only Caddy publishes ports. PostgreSQL, the API, worker, and schedulers remain on the Docker network.

## Local verification

```sh
python3.12 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest
cp .env.example .env
# Replace every placeholder, then:
./scripts/render-config.sh
docker compose config
docker compose up -d
curl https://jawnix.com/api/readyz
```

The PostgreSQL concurrency acceptance test is intentionally separate from unit tests:

```sh
docker run --rm --network jawnix_private \
  -e DATABASE_URL="$DATABASE_URL" \
  -v "$PWD:/src" -w /src jawnix-api \
  python scripts/postgres_concurrency_acceptance.py
```

## Data terminal

The worker and CLI call the same allocator.

```sh
python -m jawnix_data redistribute --request-id UUID
python -m jawnix_data sync-scrapers
python -m jawnix_data sync-scrapers --source NAME
python -m jawnix_data inventory --states TX,FL
python -m jawnix_data retry-delivery --request-id UUID
```

Migration and production operations are documented in [OPERATIONS.md](docs/OPERATIONS.md). Copy `.env.example` for the complete secret/configuration contract. Never commit `.env` or generated `config.js`.

## Allocation rules

Allocation is all-or-nothing and transactionally locks rows with `SKIP LOCKED`. A phone is never repeated to an agent; members of one agency share permanent no-repeat history. Other recipients may receive it only after the seven-day global cooldown. Never-distributed rows sort first, followed by the oldest distribution date. A retry after generation reuses the same events and CSV.
