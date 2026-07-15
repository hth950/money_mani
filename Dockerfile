FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv

RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential gcc g++ \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip wheel \
    && pip install -r /tmp/requirements.txt


FROM python:3.12-slim-bookworm AS runtime

ARG APP_GIT_SHA=unknown
ARG APP_GID=1000
ARG APP_UID=1000

ENV APP_GIT_SHA="$APP_GIT_SHA" \
    HOME=/home/money-mani \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Seoul

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ca-certificates \
        libgomp1 \
        libxml2 \
        libxslt1.1 \
        tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "$APP_GID" money-mani \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --create-home \
        --shell /usr/sbin/nologin money-mani

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=money-mani:money-mani . /app

RUN mkdir -p \
        /app/data \
        /app/output \
        /app/backups \
        /home/money-mani/.money_mani \
    && touch /app/MEMORY.md \
    && chown -R money-mani:money-mani \
        /app/data \
        /app/output \
        /app/backups \
        /app/MEMORY.md \
        /home/money-mani

USER money-mani

EXPOSE 31234
STOPSIGNAL SIGTERM

CMD ["python", "deploy/hermes/run_web.py"]
