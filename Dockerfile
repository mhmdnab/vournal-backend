# Vournal backend — Docker deploy.
#
# Why Docker: prisma-client-py ships a native query-engine binary compiled for a
# specific OpenSSL (here `debian-openssl-3.0.x`). Render's NATIVE Python runtime
# has an OpenSSL/libc that the binary couldn't execute ("could not be executed"),
# so we pin the OS ourselves. `python:3.12-slim-bookworm` is Debian 12 → OpenSSL
# 3.0.x, which matches the engine exactly. The client + engine are generated INTO
# the image at build, so they're guaranteed present and runnable at runtime.

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ca-certificates for the HTTPS downloads prisma-client-py makes (its Node CLI +
# engine binaries). OpenSSL 3 is already in the base image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Python deps first, so this layer caches unless requirements.txt changes.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# App code + Prisma schema, then bake the generated client and the engine
# binaries into the image (fetch + generate run in the SAME environment that
# runs at runtime, so every path lines up and the engine is runnable).
COPY . .
RUN python -m prisma py fetch \
 && python -m prisma generate

# At startup: apply any pending migrations (idempotent — a no-op when the DB is
# already up to date), then serve. DB creds + $PORT come from Render's env.
CMD ["sh", "-c", "python -m prisma migrate deploy && python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
