FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends caddy libreoffice \
  && rm -rf /var/lib/apt/lists/*

COPY app.py index.html login.html portal.html portal-accept.html theme.css config.example.js supabase-schema.sql README.md Caddyfile railway-start.sh ./
COPY templates ./templates

RUN chmod +x /app/railway-start.sh

EXPOSE 8080

CMD ["/app/railway-start.sh"]
