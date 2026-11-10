FROM node:20-alpine AS frontend-build

WORKDIR /workspace
COPY package.json package-lock.json ./
RUN npm ci --include=dev
COPY frontend ./frontend
RUN npm run build

FROM python:3.11-slim-bookworm AS backend

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HOME=/home/docuflow \
    TMPDIR=/tmp

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system docuflow \
    && useradd --system --gid docuflow --create-home --home-dir /home/docuflow docuflow

COPY backend/requirements.txt ./backend/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r backend/requirements.txt \
    && python -m pip install --no-cache-dir --no-deps rapid-table==3.0.2 \
    && python -c "import rapidocr, rapid_table; from pathlib import Path; (Path(rapidocr.__file__).parent / 'models').mkdir(parents=True, exist_ok=True); (Path(rapid_table.__file__).parent / 'models').mkdir(parents=True, exist_ok=True)"

COPY backend ./backend
RUN mkdir -p /app/backend/storage/work/ocr-cache \
    && chown -R docuflow:docuflow /app/backend/storage /home/docuflow \
       /usr/local/lib/python3.11/site-packages/rapidocr/models \
       /usr/local/lib/python3.11/site-packages/rapid_table/models

USER docuflow:docuflow
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).read()" || exit 1
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

FROM nginx:1.27-alpine AS frontend
COPY --from=frontend-build /workspace/frontend/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/ || exit 1
