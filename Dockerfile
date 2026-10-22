FROM python:3.11-slim@sha256:cce12316475d7d56d605f76e1d811d0dc4e7227f2d4afd29951521264f504ff8 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements-docker.lock ./
RUN python -m pip install -r requirements-docker.lock
RUN groupadd --gid 10001 ops \
    && useradd --uid 10001 --gid ops --create-home --shell /usr/sbin/nologin ops \
    && mkdir -p /state/sessions /state/index \
    && chown -R ops:ops /state
COPY agent/ ./agent/
COPY server.py web_demo_server.py web_chat.html mcp_server.py mcp_server_docs.py ./
COPY data/monitoring.json ./data/monitoring.json
COPY data/runbooks/ ./data/runbooks/
COPY sh/build_runbook_index.py ./sh/build_runbook_index.py
COPY docker/ ./docker/
USER 10001:10001
ENTRYPOINT ["python", "docker/entrypoint.py"]
CMD ["backend"]

FROM runtime AS tests
COPY tests/ ./tests/
CMD ["tests"]
