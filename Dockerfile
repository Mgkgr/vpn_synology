# Reviewed 2026-07-19. Update deliberately, then run CI and build before deploy.
FROM node:22.23.1-alpine3.24@sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2 AS frontend-builder
WORKDIR /frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

FROM python:3.12.13-slim@sha256:c3d81d25b3154142b0b42eb1e61300024426268edeb5b5a26dd7ddf64d9daf28 AS dashboard
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 dashboard \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin dashboard \
    && install -d -m 0755 /gateway \
    && install -d -o dashboard -g dashboard -m 0750 /gateway/rules /geodata

COPY backend/app ./app
COPY backend/pyproject.toml ./
RUN pip install --no-cache-dir .

COPY --chown=10001:10001 --from=frontend-builder /frontend/dist ./app/static

EXPOSE 8080
USER 10001:10001
CMD ["uvicorn", "app.main:app", "--app-dir", "/app", "--host", "0.0.0.0", "--port", "8080"]
