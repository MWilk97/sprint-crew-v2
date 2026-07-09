FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi \
    uvicorn[standard] \
    "transformers>=4.41,<5" \
    "sentence-transformers>=3.3,<4" \
    torch --extra-index-url https://download.pytorch.org/whl/cpu

COPY app.py /app/app.py

ENV EMBED_MODEL_ID=jinaai/jina-embeddings-v2-base-code
ENV HF_HOME=/root/.cache/huggingface
ENV PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --retries=5 --start-period=120s \
    CMD curl -sf http://127.0.0.1:8080/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
