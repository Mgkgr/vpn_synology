FROM node:22-alpine AS frontend-builder
WORKDIR /frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS dashboard
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 dashboard \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin dashboard

COPY backend/app ./app
COPY backend/pyproject.toml ./
RUN pip install --no-cache-dir .

COPY --chown=10001:10001 --from=frontend-builder /frontend/dist ./app/static

EXPOSE 8080
USER 10001:10001
CMD ["uvicorn", "app.main:app", "--app-dir", "/app", "--host", "0.0.0.0", "--port", "8080"]
