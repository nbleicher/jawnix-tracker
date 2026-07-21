FROM postgres:18 AS postgres-client

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends curl ca-certificates postgresql-client restic \
  && rm -rf /var/lib/apt/lists/*

COPY --from=postgres-client /usr/lib/postgresql/18 /usr/lib/postgresql/18
RUN for tool in pg_dump pg_restore pg_basebackup psql pg_isready createdb dropdb; do \
      ln -sf "/usr/lib/postgresql/18/bin/$tool" "/usr/local/bin/$tool"; \
    done

COPY pyproject.toml ./
COPY jawnix ./jawnix
COPY jawnix_data ./jawnix_data
COPY alembic.ini ./
COPY alembic ./alembic
RUN pip install --no-cache-dir .

COPY admin.html login.html portal.html portal-accept.html theme.css config.example.js ./static/
COPY index.html app.py supabase-schema.sql ./legacy/
COPY docker-entrypoint.sh ./
COPY ops ./ops
RUN chmod +x /app/docker-entrypoint.sh \
  && mkdir -p /srv/jawnix/batches

EXPOSE 8001

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "jawnix.api:app", "--host", "0.0.0.0", "--port", "8001", "--proxy-headers"]
