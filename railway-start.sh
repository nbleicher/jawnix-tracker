#!/usr/bin/env sh
set -eu

: "${PORT:=8080}"
: "${JAWNIX_WORKSPACE_ID:=default}"
: "${JAWNIX_LEADS_TABLE:=jawnix_leads}"
: "${JAWNIX_SETTINGS_TABLE:=jawnix_settings}"
: "${CADDY_BIN:=caddy}"
: "${APP_DIR:=/app}"
: "${JAWNIX_ALLOW_UNPROTECTED:=false}"
: "${JAWNIX_BASIC_AUTH_USER:=}"
: "${JAWNIX_BASIC_AUTH_HASH:=}"

: "${JAWNIX_SUPABASE_URL:=}"
: "${JAWNIX_SUPABASE_ANON_KEY:=}"

if [ -z "$JAWNIX_SUPABASE_URL" ] || [ -z "$JAWNIX_SUPABASE_ANON_KEY" ]; then
  echo "Warning: JAWNIX_SUPABASE_URL and JAWNIX_SUPABASE_ANON_KEY are not set; serving app with empty database config." >&2
fi

if [ "$JAWNIX_ALLOW_UNPROTECTED" != "true" ]; then
  if [ -z "$JAWNIX_BASIC_AUTH_USER" ] || [ -z "$JAWNIX_BASIC_AUTH_HASH" ]; then
    echo "Error: set JAWNIX_BASIC_AUTH_USER and JAWNIX_BASIC_AUTH_HASH, or set JAWNIX_ALLOW_UNPROTECTED=true for an intentionally unprotected deployment." >&2
    exit 1
  fi
fi

js_escape() {
  printf "%s" "$1" | sed "s/\\\\/\\\\\\\\/g; s/'/\\\\'/g"
}

cat > "$APP_DIR/config.js" <<EOF
window.JAWNIX_CONFIG = {
  supabaseUrl: '$(js_escape "$JAWNIX_SUPABASE_URL")',
  supabaseAnonKey: '$(js_escape "$JAWNIX_SUPABASE_ANON_KEY")',
  workspaceId: '$(js_escape "$JAWNIX_WORKSPACE_ID")',
  leadsTable: '$(js_escape "$JAWNIX_LEADS_TABLE")',
  settingsTable: '$(js_escape "$JAWNIX_SETTINGS_TABLE")',
};
EOF

cat > "$APP_DIR/Caddyfile.generated" <<EOF
{
	admin off
	auto_https off
}

0.0.0.0:$PORT {
	root * $APP_DIR

	handle /healthz {
		respond "ok" 200
	}
EOF

if [ "$JAWNIX_ALLOW_UNPROTECTED" != "true" ]; then
  cat >> "$APP_DIR/Caddyfile.generated" <<EOF

	@protected not path /healthz
	basic_auth @protected {
		$JAWNIX_BASIC_AUTH_USER $JAWNIX_BASIC_AUTH_HASH
	}
EOF
fi

cat >> "$APP_DIR/Caddyfile.generated" <<'EOF'

	try_files {path} /index.html
	file_server

	header {
		X-Content-Type-Options nosniff
		Referrer-Policy no-referrer-when-downgrade
	}
}
EOF

exec "$CADDY_BIN" run --config "$APP_DIR/Caddyfile.generated" --adapter caddyfile
