FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENTPOST_DB=/data/agentpost.sqlite \
    AGENTPOST_HOST=0.0.0.0 \
    AGENTPOST_PORT=8765

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agentpost ./agentpost

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin agentpost \
    && mkdir /data && chown agentpost /data
USER agentpost
VOLUME /data
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-m", "agentpost.healthcheck"]

CMD ["python", "-m", "agentpost.server"]
