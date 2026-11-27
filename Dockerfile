FROM node:22-bookworm-slim AS frontend
WORKDIR /src
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ .
RUN npm run build

FROM python:3.12-slim AS app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8000
WORKDIR /usr/src/goat
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --system --uid 10001 --no-create-home goat
COPY --chown=10001:10001 goat/ ./goat/
COPY --chown=10001:10001 migrations/ ./migrations/
COPY --chown=10001:10001 start.py alembic.ini ./
COPY --from=frontend --chown=10001:10001 /src/dist ./frontend/dist
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/health', timeout=3)"
CMD ["python", "start.py"]
