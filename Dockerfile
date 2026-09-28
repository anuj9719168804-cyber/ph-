FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Runtime dependencies: FFmpeg + libraries required by PhantomJS 2.1.1.
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget ca-certificates bzip2 fontconfig \
        ffmpeg \
        libfreetype6 libx11-6 libxext6 libxrender1 \
        libjpeg62-turbo libpng16-16 libfontconfig1 libglib2.0-0 \
        libnss3 libnspr4 libxss1 libasound2 \
    && rm -rf /var/lib/apt/lists/*

# PhantomJS 2.1.1 is an old binary and expects the old libssl ABI.
# Use the archived Debian Jessie package rather than the broken/nonexistent
# stretch URL. Do NOT set OPENSSL_CONF: the modern OpenSSL 3 provider config
# is incompatible with PhantomJS's bundled OpenSSL 1.0 stack.
RUN set -eux; \
    wget -q -O /tmp/libssl1.0.0.deb \
      https://archive.debian.org/debian-security/pool/updates/main/o/openssl/libssl1.0.0_1.0.1t-1+deb8u12_amd64.deb; \
    dpkg -i /tmp/libssl1.0.0.deb; \
    rm -f /tmp/libssl1.0.0.deb

# Install PhantomJS 2.1.1. The wrapper deliberately sets the Qt platform
# to "phantom" because this build ships that headless platform plugin;
# "offscreen" causes the Qt platform-plugin error seen previously.
RUN set -eux; \
    wget -q -O /tmp/phantomjs.tar.bz2 \
      https://github.com/Medium/phantomjs/releases/download/v2.1.1/phantomjs-2.1.1-linux-x86_64.tar.bz2; \
    tar -xjf /tmp/phantomjs.tar.bz2 -C /tmp; \
    mv /tmp/phantomjs-2.1.1-linux-x86_64/bin/phantomjs /usr/local/bin/phantomjs.real; \
    chmod +x /usr/local/bin/phantomjs.real; \
    rm -rf /tmp/phantomjs*; \
    printf '%s\n' '#!/bin/sh' \
      'export QT_QPA_PLATFORM=phantom' \
      'exec /usr/local/bin/phantomjs.real "$@"' \
      > /usr/local/bin/phantomjs; \
    chmod +x /usr/local/bin/phantomjs; \
    /usr/local/bin/phantomjs --version

COPY . .
RUN chmod +x entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
