FROM caddy:2-alpine

WORKDIR /app

COPY index.html config.example.js supabase-schema.sql README.md Caddyfile railway-start.sh ./

RUN chmod +x /app/railway-start.sh

CMD ["/app/railway-start.sh"]
