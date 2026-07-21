# VPS deployment, migration, cutover, and rollback

## Protected baseline

The pre-refactor production baseline is commit `119d072bcf1a3ccc961847702e8a16ce1c109d52`, tagged and pushed as `backup/pre-vps-batch-platform-20260721T190329Z`. The linked feature worktree was at `6a338a184b555dcab97680088f3a6b59af0cfd4d`.

The verified local recovery set is outside Git at:

```text
.context/backups/20260721T190329Z/
```

It contains a verified `git bundle --all`, source archives, encrypted Railway variables, an encrypted Supabase application-data export, source checksums, and restore instructions. Its encryption password is in the macOS Keychain service `jawnix-backup-20260721T190329Z`, account `noahbleicher`. Supabase Auth was not exported or changed.

Do not stop Railway or delete old Supabase application tables during provisioning, migration, cutover, or the first 48 hours after cutover.

## VPS provisioning

1. Provision a Linux VPS with Docker Engine, the Compose plugin, SSH-key access, and a firewall allowing only TCP 22/80/443 and UDP 443.
2. Clone this branch, copy `.env.example` to `.env`, and replace every placeholder. Generate independent database, session, and Restic passwords.
3. Configure a private Slack app channel, interactive request URL `https://jawnix.com/api/integrations/slack/actions`, bot token, signing secret, and comma-separated authorized Slack user IDs.
4. Verify `jawnix.com` in Resend, configure `batches@jawnix.com`, and set the webhook to `https://jawnix.com/api/integrations/resend/webhook` for delivered, bounced, and complained events.
5. Configure a private encrypted Restic repository and credentials. Do not reuse the database or session secret as its password. The backup service writes a logical dump daily, includes archived WAL, creates a physical base backup on the configured UTC weekday, and expires local/offsite material after 14 days.
6. Run `./scripts/render-config.sh`, then `docker compose config` and `docker compose build`.
7. Run `docker compose up -d postgres`, `docker compose run --rm migrate`, then `docker compose up -d`.
8. Confirm `/api/healthz`, `/api/readyz`, container health/logs, PostgreSQL `archive_mode`, and a Restic snapshot before importing production data. Force one initial physical base backup with `docker compose run --rm -e JAWNIX_FORCE_BASEBACKUP=true backup /app/ops/backup.sh`.

`config.js` contains only the Supabase browser URL and publishable/anon key. Service-role, Slack, Resend, PostgreSQL, and Restic secrets remain server-side.

The PostgreSQL initialization hook enables password-authenticated replication only for physical backups on the private Docker network. If attaching this stack to an already-initialized PostgreSQL volume, add the equivalent `host replication` rule to `pg_hba.conf` and reload PostgreSQL before forcing the first base backup.

## Source migration

Treat the Mac `dat` directory as read-only. Copy pinned source files to a staging volume on the VPS; never mount or run migration commands against the originals. The accepted source snapshot is:

| Source | Rows | SHA-256 |
|---|---:|---|
| `util/archive/manifest.csv` | 6,539,606 | `caf517926c53fe17ba1218742d18adefbd157147a08656bc0ece8388f925f3b4` |
| `global/all_combined.csv` | 4,511,654 | `3ac5bb7b79454d438562f96bb9e7511e849e86846604c271e27e022401fa1dde` |
| `health_leads/data/leads.db` | 5,735,955 rows / 2,305,030 distinct phones | `00ccc0eba3362da64fb47817efeed722dde824f7d30221108fe6e1103faf7dea` |

Run imports in this order:

```sh
docker compose run --rm -v /srv/jawnix/migration:/migration:ro api python -m jawnix_data import-config /migration/config.json
docker compose run --rm -v /srv/jawnix/migration:/migration:ro api python -m jawnix_data import-manifest \
  /migration/manifest.csv \
  --expected-sha256 caf517926c53fe17ba1218742d18adefbd157147a08656bc0ece8388f925f3b4
docker compose run --rm -v /srv/jawnix/migration:/migration:ro api python -m jawnix_data import-history /migration/redistribution
docker compose run --rm -v /srv/jawnix/migration:/migration:ro api python -m jawnix_data import-scraper-db \
  /migration/leads.db \
  --expected-sha256 00ccc0eba3362da64fb47817efeed722dde824f7d30221108fe6e1103faf7dea
docker compose run --rm -v /srv/jawnix/migration:/migration:ro api python -m jawnix_data import-supabase /migration/supabase-export
```

The config import intentionally blocks on invalid configured states. The pinned config contains `IO` and `CN`; correct those in a staging copy only, document the chosen corrections, and rerun. Do not guess or edit the original.

Manifest data wins on phone collisions. Scraper-only rows use their valid source state, then phone-derived state; titles fall back through company, full name, and niche. Malformed records enter `quarantined_rows`. Migration checksums and counts enter `migration_audits`, so identical reruns are skipped.

After import, reconcile table totals, inventory by state, distinct normalized phones, agent and agency histories, quarantine reasons, source provenance, profiles, and pending requests. Run `python -m jawnix_data inventory` and state-specific dry runs before enabling customers.

## Customer mapping and acceptance

Sign in with an existing Supabase admin, open Recipients, and synchronize Supabase users. Every proposed customer-to-agent mapping starts unconfirmed. Manually select and confirm each agent; requests remain blocked until confirmation.

Before cutover, prove:

- existing Supabase login and VPS session exchange;
- profile state save and all/subset request validation;
- 1 and 100,000-row boundaries;
- Slack Approve/Reject, authorized-user checks, replay behavior, and duplicate clicks;
- exact CSV row count, unique phones, state scope, `phone,title` header, and recorded checksum;
- shortage creates zero distribution events and Retry works after inventory arrives;
- generation failure commits no allocation and delivery failure reuses its artifact;
- Resend delivery, bounce, and complaint webhooks update state and Slack;
- two concurrent allocations have no overlapping phone IDs;
- staging restore from Restic and `git bundle verify` both pass.

For the 10-million-row performance gate, run the guarded harness against a disposable staging database. Acceptance is allocation plus CSV generation in under five minutes without API health failures. Never run it in production.

```sh
JAWNIX_ALLOW_SYNTHETIC_LOAD_TEST=YES \
JAWNIX_LOAD_TEST_HEALTH_URL=https://staging.jawnix.com/api/readyz \
python scripts/postgres_load_test.py \
  --inventory-rows 10000000 --request-rows 100000 --max-seconds 300
```

## Cutover

1. Keep Railway live while the VPS is populated and tested.
2. Lower DNS TTL at least one previous TTL window before cutover.
3. Announce a short request/redistribution pause. Disable only new requests and local redistribution; leave login and history available.
4. Export/import the final Supabase and source-data delta, reconcile counts, and verify every active customer mapping.
5. Take and verify a VPS database backup and Restic snapshot.
6. Change the `jawnix.com` A/AAAA records to the VPS. Confirm Caddy certificate issuance and the complete login → request → Slack approval → CSV email → delivered flow.
7. Re-enable requests and monitor API errors, worker failures, queued/running jobs, PostgreSQL locks/storage, Slack notifications, Resend bounces/complaints, backups, and DNS from multiple resolvers for 48 hours.
8. After 48 clean hours, Railway may be scaled down only after explicit approval. Do not delete Railway, Supabase tables, the rollback tag, or the recovery bundle as part of this change.

## Rollback

1. Pause new VPS requests and worker processing.
2. Restore the prior `jawnix.com` DNS records for Railway.
3. If Railway configuration changed, decrypt and restore `railway-variables.json.enc` from the protected recovery set.
4. Redeploy `backup/pre-vps-batch-platform-20260721T190329Z` (or restore from the verified bundle/archive).
5. Verify login, admin, requests, and legacy Supabase data on Railway.
6. Preserve the VPS database and logs for reconciliation; do not merge them back into Supabase automatically.

The VPS migration is additive. Rollback does not overwrite or delete old Supabase application data.
