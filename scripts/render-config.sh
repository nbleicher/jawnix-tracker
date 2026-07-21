#!/usr/bin/env sh
set -eu

: "${JAWNIX_SUPABASE_URL:?set JAWNIX_SUPABASE_URL}"
: "${JAWNIX_SUPABASE_ANON_KEY:?set JAWNIX_SUPABASE_ANON_KEY}"

escape_js() {
  printf '%s' "$1" | sed "s/\\\\/\\\\\\\\/g; s/'/\\\\'/g"
}

umask 077
{
  echo "window.JAWNIX_CONFIG = {"
  echo "  supabaseUrl: '$(escape_js "$JAWNIX_SUPABASE_URL")',"
  echo "  supabaseAnonKey: '$(escape_js "$JAWNIX_SUPABASE_ANON_KEY")',"
  echo "  billingEnabled: false,"
  echo "};"
} > config.js
