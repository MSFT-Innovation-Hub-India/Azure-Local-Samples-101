FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/index.html ./
COPY frontend/src ./src
RUN npm run build

FROM python:3.12-slim-bookworm AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock.txt ./
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && apt-get clean \
    && pip install --no-cache-dir -r requirements.lock.txt \
    && useradd --uid 10001 --create-home assistant
COPY assistant ./assistant
COPY --from=frontend /build/dist ./frontend/dist
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "assistant.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

FROM app AS embeddings
USER root
ENV MODEL_CACHE=/models HF_HUB_DISABLE_TELEMETRY=1
RUN mkdir /models && chown 10001:10001 /models
USER 10001:10001
RUN python -m assistant.embeddings
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
EXPOSE 8001
CMD ["python", "-m", "uvicorn", "assistant.embeddings:app", "--host", "0.0.0.0", "--port", "8001", "--no-access-log"]
