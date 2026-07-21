#!/usr/bin/env sh
set -eu

if [ "${JAWNIX_RUN_MIGRATIONS:-false}" = "true" ]; then
  alembic upgrade head
fi

exec "$@"

