#!/usr/bin/env sh
set -eu

# pg_basebackup uses the replication protocol. This remains reachable only on
# the private Docker network and still requires the PostgreSQL password.
printf '%s\n' 'host replication all all scram-sha-256' >> "$PGDATA/pg_hba.conf"
