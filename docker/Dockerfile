FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --upgrade pip \
 && pip install ".[server]"

RUN mkdir -p /var/lib/lexalign /etc/lexalign

ENV BIND_HOST=0.0.0.0 \
    BIND_PORT=8765 \
    DATABASE_PATH=/var/lib/lexalign/lexalign.sqlite \
    REGISTRY_PATH=/etc/lexalign/registry.json

EXPOSE 8765

CMD ["lex-align-server", "serve"]
