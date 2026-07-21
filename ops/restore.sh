#!/usr/bin/env sh
set -eu

if [ "${RESTORE_CONFIRM:-}" != "YES" ]; then
  echo "Set RESTORE_CONFIRM=YES to restore the selected dump." >&2
  exit 2
fi

if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
  echo "usage: RESTORE_CONFIRM=YES restore.sh /path/to/jawnix.dump" >&2
  exit 2
fi

pg_restore --clean --if-exists --no-owner --no-acl --dbname="${PGDATABASE:-jawnix}" "$1"

