FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt
# The two embedding models for knowledge retrieval, fetched once at build time by commit, so the
# running container never reaches out for one (HF_HUB_OFFLINE below). Each repository's README
# carries its licence and stays with the files. okf_retrieval/config.py holds the same revisions
# and directories; a test pins them equal.
RUN python -c "from huggingface_hub import snapshot_download as d; \
d('Qdrant/bge-small-en-v1.5-onnx-Q', revision='aa8f8b060edb00e03bfdd08813a2949946c8ba55', local_dir='/opt/models/bge'); \
d('Qdrant/bm25', revision='22b8d2af71a76161e18dd432d2cee0eefa66e412', local_dir='/opt/models/bm25', allow_patterns=['*.txt','*.json','README.md'])" \
 && chmod -R a+rX /opt/models
ENV HF_HUB_OFFLINE=1
COPY . .
# Run as an unprivileged user; the app files stay root-owned and read-only to it.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app
# A new named volume mounted at /data copies this ownership on first use, so the
# non-root user can write its durable state file there.
RUN mkdir -p /data && chown 10001:10001 /data
ENV PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "broker_engine.main:app", "--host", "0.0.0.0", "--port", "8080"]
