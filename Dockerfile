FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    NBA_AGENT_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY nba_agent ./nba_agent
COPY run.py .

# Settings, conversation state and caches live here. Mount a persistent volume
# at /data, or they're lost every time the container is replaced.
RUN useradd --create-home app && mkdir -p /data && chown app /data
USER app
VOLUME ["/data"]

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"
CMD ["python", "run.py", "web"]
