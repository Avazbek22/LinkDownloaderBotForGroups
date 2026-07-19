FROM python:3.12-slim

ARG NODE_VERSION=24.18.0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data \
    LOGS_DIR=/app/logs \
    OUTPUT_FOLDER=/app/data/cache

WORKDIR /app

RUN set -eux; \
    apt-get update -y; \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates gosu curl xz-utils; \
    case "$(dpkg --print-architecture)" in \
      amd64) node_arch='x64' ;; \
      arm64) node_arch='arm64' ;; \
      *) echo 'Unsupported architecture' >&2; exit 1 ;; \
    esac; \
    node_archive="node-v${NODE_VERSION}-linux-${node_arch}.tar.xz"; \
    curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/${node_archive}"; \
    curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt"; \
    grep " ${node_archive}$" SHASUMS256.txt | sha256sum -c -; \
    tar -xJf "$node_archive" -C /usr/local/bin --strip-components=2 "node-v${NODE_VERSION}-linux-${node_arch}/bin/node"; \
    node --version; \
    rm -f "$node_archive" SHASUMS256.txt; \
    apt-get purge -y --auto-remove curl xz-utils; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN sed '/^yt-dlp/d' /app/requirements.txt > /tmp/requirements-base.txt \
    && pip install --no-cache-dir -r /tmp/requirements-base.txt

# The updater changes this argument to refresh yt-dlp without invalidating the
# slower OS and stable Python dependency layers.
ARG YTDLP_CACHEBUST=initial
RUN echo "$YTDLP_CACHEBUST" >/tmp/ytdlp-cachebust \
    && pip install --no-cache-dir --upgrade 'yt-dlp[default,curl-cffi]'

COPY app /app/app
COPY main.py config.py /app/
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid bot --no-create-home --home-dir /app bot \
    && mkdir -p /app/data/cache /app/logs \
    && chown -R bot:bot /app/data /app/logs

RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh

ENV XDG_CACHE_HOME=/tmp/.cache

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-u", "/app/main.py"]
