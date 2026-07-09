#!/usr/bin/env sh
set -eu

: "${PORT:=8080}"
: "${JAWNIX_API_PORT:=8001}"
: "${JAWNIX_WORKSPACE_ID:=default}"
: "${JAWNIX_LEADS_TABLE:=jawnix_leads}"
: "${JAWNIX_SETTINGS_TABLE:=jawnix_settings}"
: "${JAWNIX_INVOICE_VOIDS_TABLE:=jawnix_invoice_voids}"
: "${JAWNIX_INVOICE_RECORDS_TABLE:=jawnix_invoice_records}"
: "${JAWNIX_EXPENSES_TABLE:=jawnix_expenses}"
: "${JAWNIX_MISC_INCOME_TABLE:=jawnix_misc_income}"
: "${CADDY_BIN:=caddy}"
: "${APP_DIR:=/app}"
: "${JAWNIX_INVOICE_DIR:=$APP_DIR/invoices}"
: "${JAWNIX_ALLOW_UNPROTECTED:=false}"
: "${JAWNIX_BASIC_AUTH_USER:=}"
: "${JAWNIX_BASIC_AUTH_PASSWORD:=}"

: "${JAWNIX_SUPABASE_URL:=}"
: "${JAWNIX_SUPABASE_ANON_KEY:=}"

if [ -z "$JAWNIX_SUPABASE_URL" ] || [ -z "$JAWNIX_SUPABASE_ANON_KEY" ]; then
  echo "Warning: JAWNIX_SUPABASE_URL and JAWNIX_SUPABASE_ANON_KEY are not set; serving app with empty database config." >&2
fi

if [ "$JAWNIX_ALLOW_UNPROTECTED" != "true" ]; then
  if [ -z "$JAWNIX_BASIC_AUTH_USER" ] || [ -z "$JAWNIX_BASIC_AUTH_PASSWORD" ]; then
    echo "Error: set JAWNIX_BASIC_AUTH_USER and JAWNIX_BASIC_AUTH_PASSWORD, or set JAWNIX_ALLOW_UNPROTECTED=true for an intentionally unprotected deployment." >&2
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
  invoiceVoidsTable: '$(js_escape "$JAWNIX_INVOICE_VOIDS_TABLE")',
  invoiceRecordsTable: '$(js_escape "$JAWNIX_INVOICE_RECORDS_TABLE")',
  expensesTable: '$(js_escape "$JAWNIX_EXPENSES_TABLE")',
  miscIncomeTable: '$(js_escape "$JAWNIX_MISC_INCOME_TABLE")',
};
EOF

mkdir -p "$JAWNIX_INVOICE_DIR"
python3 "$APP_DIR/app.py" &
api_pid="$!"

cleanup() {
  kill "$api_pid" 2>/dev/null || true
  if [ "${caddy_pid:-}" ]; then
    kill "$caddy_pid" 2>/dev/null || true
  fi
}
trap cleanup INT TERM EXIT

cat > "$APP_DIR/Caddyfile.generated" <<EOF
{
	admin off
	auto_https off
}

:$PORT {
	bind 0.0.0.0
	root * $APP_DIR

	handle /healthz {
		respond "ok" 200
	}

	@api_preflight {
		path /api/*
		method OPTIONS
	}
	handle @api_preflight {
		reverse_proxy 127.0.0.1:$JAWNIX_API_PORT
	}

EOF

if [ "$JAWNIX_ALLOW_UNPROTECTED" != "true" ]; then
  cat >> "$APP_DIR/Caddyfile.generated" <<EOF

	handle /api/auth/* {
		reverse_proxy 127.0.0.1:$JAWNIX_API_PORT
	}

	handle /api/* {
		forward_auth 127.0.0.1:$JAWNIX_API_PORT {
			uri /api/auth/check
		}

		reverse_proxy 127.0.0.1:$JAWNIX_API_PORT
	}

	handle /login.html {
		file_server
	}

	handle /login {
		redir * /login.html 308
	}

	handle {
		@no_session not header Cookie *jawnix_session=*
		redir @no_session /login.html 303

		forward_auth 127.0.0.1:$JAWNIX_API_PORT {
			uri /api/auth/check
			header_up X-Jawnix-Auth-Redirect /login.html
		}

		try_files {path} /index.html
		file_server
	}
EOF
else
  cat >> "$APP_DIR/Caddyfile.generated" <<EOF

	handle /api/* {
		reverse_proxy 127.0.0.1:$JAWNIX_API_PORT
	}

	handle {
		try_files {path} /index.html
		file_server
	}
EOF
fi

cat >> "$APP_DIR/Caddyfile.generated" <<'EOF'

	header {
		X-Content-Type-Options nosniff
		Referrer-Policy no-referrer-when-downgrade
	}
}
EOF

"$CADDY_BIN" run --config "$APP_DIR/Caddyfile.generated" --adapter caddyfile &
caddy_pid="$!"
wait "$caddy_pid"
