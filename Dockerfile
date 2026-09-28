FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Runtime packages: FFmpeg + libraries needed by the PhantomJS 2.1.1 binary.
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget ca-certificates bzip2 fontconfig ffmpeg \
        libfreetype6 libx11-6 libxext6 libxrender1 libjpeg62-turbo libpng16-16 \
        libfontconfig1 libglib2.0-0 libnss3 libdbus-1-3 libgtk2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install PhantomJS 2.1.1.  The upstream binary expects OpenSSL 1.0 libraries.
# Do NOT use dpkg -i here: the archived Debian package has a historical
# `multiarch-support` pre-dependency that does not exist on bookworm.
RUN set -eux; \
    wget -q -O /tmp/phantomjs.tar.bz2 \
      https://github.com/Medium/phantomjs/releases/download/v2.1.1/phantomjs-2.1.1-linux-x86_64.tar.bz2 \
      || wget -q -O /tmp/phantomjs.tar.bz2 \
      https://bitbucket.org/ariya/phantomjs/downloads/phantomjs-2.1.1-linux-x86_64.tar.bz2; \
    tar -xjf /tmp/phantomjs.tar.bz2 -C /tmp; \
    mv /tmp/phantomjs-2.1.1-linux-x86_64/bin/phantomjs /usr/local/bin/phantomjs.real; \
    chmod +x /usr/local/bin/phantomjs.real; \
    rm -rf /tmp/phantomjs*; \
    wget -q -O /tmp/libssl1.0.0.deb \
      https://archive.debian.org/debian-security/pool/updates/main/o/openssl/libssl1.0.0_1.0.1t-1+deb8u12_amd64.deb; \
    mkdir -p /tmp/libssl-extract; \
    dpkg-deb -x /tmp/libssl1.0.0.deb /tmp/libssl-extract; \
    find /tmp/libssl-extract -type f -name 'libssl.so.1.0.0' -exec cp -v {} /usr/lib/x86_64-linux-gnu/ \;; \
    find /tmp/libssl-extract -type f -name 'libcrypto.so.1.0.0' -exec cp -v {} /usr/lib/x86_64-linux-gnu/ \;; \
    ldconfig; \
    rm -rf /tmp/libssl1.0.0.deb /tmp/libssl-extract

# This PhantomJS build contains the Qt platform plugin named "phantom".
RUN printf '#!/bin/sh\nexport QT_QPA_PLATFORM=phantom\nexec /usr/local/bin/phantomjs.real "$@"\n' \
      > /usr/local/bin/phantomjs \
    && chmod +x /usr/local/bin/phantomjs \
    && phantomjs --version

COPY . .
RUN chmod +x entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["./entrypoint.sh"]
