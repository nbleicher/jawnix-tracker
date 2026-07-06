# Jawnix Lead Tracker

This is a static web app backed by Supabase.

Live app: https://jawnix-tracker-production.up.railway.app

## Supabase setup

1. Create a Supabase project.
2. Run `supabase-schema.sql` in the Supabase SQL editor.
3. For local static testing, copy `config.example.js` to `config.js`.
4. Fill in your Supabase project URL and anon key in `config.js`.

Current Supabase project ref: `xlefqzzhkfmzoowmysxs`.

## Railway deployment

This repo is configured for Railway with Caddy and Nixpacks:

- `railway.json` pins the Railway deploy settings.
- `Dockerfile` installs Caddy and LibreOffice.
- `Caddyfile` serves the static app and proxies invoice API requests.
- `railway-start.sh` creates `config.js` from Railway Variables, starts the invoice API, and enables Basic Auth by default.

Set these Railway Variables on the service:

```sh
JAWNIX_SUPABASE_URL=https://YOUR-PROJECT.supabase.co
JAWNIX_SUPABASE_ANON_KEY=YOUR-SUPABASE-ANON-OR-PUBLISHABLE-KEY
JAWNIX_WORKSPACE_ID=default
JAWNIX_LEADS_TABLE=jawnix_leads
JAWNIX_SETTINGS_TABLE=jawnix_settings
JAWNIX_API_PORT=8001
JAWNIX_INVOICE_DIR=/app/invoices
JAWNIX_CORS_ORIGIN=*
JAWNIX_PUBLIC_BASE_URL=https://YOUR-APP-DOMAIN
STRIPE_SECRET_KEY=YOUR_STRIPE_SECRET_KEY
STRIPE_CURRENCY=usd
JAWNIX_BASIC_AUTH_USER=jawnix
JAWNIX_BASIC_AUTH_HASH=GENERATE_WITH_CADDY_HASH_PASSWORD
PORT=8080
```

The same variables are listed in `.env.example`.

Generate `JAWNIX_BASIC_AUTH_HASH` with Caddy:

```sh
docker run --rm caddy:2-alpine caddy hash-password --plaintext 'choose-a-password'
```

Only set `JAWNIX_ALLOW_UNPROTECTED=true` for a deliberately public deployment after replacing the included permissive Supabase policies with an auth-backed access model.

## Invoice PDF generation

The app includes `POST /api/generate-invoice`. It accepts the invoice JSON produced by the weekly invoice modal, creates a Stripe Checkout Session when `STRIPE_SECRET_KEY` is set, renders `templates/Jawnix_Invoice_Template.docx` with Python `zipfile`, converts it with `libreoffice`, stores the DOCX and PDF in `JAWNIX_INVOICE_DIR`, and returns the PDF as a download. The Stripe Checkout URL is embedded in the PDF and returned in the `X-Stripe-Checkout-Url` response header for the frontend's "Pay on Stripe" button.

The Docker image installs LibreOffice with apt. If generated invoices need to survive container restarts, mount persistent storage at `JAWNIX_INVOICE_DIR`.

Create and deploy with the Railway CLI:

```sh
railway login
railway init --name jawnix-tracker
railway variable set \
  JAWNIX_SUPABASE_URL=https://YOUR-PROJECT.supabase.co \
  JAWNIX_SUPABASE_ANON_KEY=YOUR-SUPABASE-ANON-OR-PUBLISHABLE-KEY \
  JAWNIX_WORKSPACE_ID=default \
  JAWNIX_LEADS_TABLE=jawnix_leads \
  JAWNIX_SETTINGS_TABLE=jawnix_settings \
  JAWNIX_API_PORT=8001 \
  JAWNIX_INVOICE_DIR=/app/invoices \
  JAWNIX_CORS_ORIGIN='*' \
  JAWNIX_PUBLIC_BASE_URL=https://YOUR-APP-DOMAIN \
  STRIPE_SECRET_KEY=YOUR_STRIPE_SECRET_KEY \
  STRIPE_CURRENCY=usd \
  JAWNIX_BASIC_AUTH_USER=jawnix \
  JAWNIX_BASIC_AUTH_HASH='PASTE-CADDY-HASH-HERE' \
  PORT=8080
railway up
railway domain
```

`config.js` is gitignored because it contains deployment-specific credentials. The anon key is safe to use in browser apps when Row Level Security policies match your access model.

The included SQL policies allow the configured browser app to read and write data with the anon key. Use this as-is only behind protected hosting or for a private/internal app. For a public app, add Supabase Auth and tighten the RLS policies before launch.
