FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.10.12 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 \
    HF_HUB_DISABLE_SYMLINKS_WARNING=1 \
    KB_DB_PATH=/data/knowledge.db KB_MODEL_CACHE_DIR=/data/models
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY kb ./kb
EXPOSE 8001
# The first start downloads the embedding model (~65 MB), hence the long start period.
HEALTHCHECK --interval=15s --timeout=5s --start-period=180s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/health')"
CMD ["uv", "run", "--no-dev", "uvicorn", "kb.api:app", "--host", "0.0.0.0", "--port", "8001"]
