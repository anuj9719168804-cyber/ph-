FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# --- PhantomJS (yt-dlp's PornHub extractor bot-challenge JS fallback) ---
# yt-dlp's PornhubIE extractor shells out to a real `phantomjs` binary
# (via its internal PhantomJSwrapper) to execute an obfuscated JS bot-
# check page PornHub serves in place of the video page on some requests
# — without it, that specific case fails with "PhantomJS not found" (see
# pornhub_resolver.py's phantomjs_available() docstring for the full
# story, and how that failure is surfaced distinctly from every other
# PornHub error). This was investigated and planned in an earlier
# session (per that session's own step-by-step summary) but never
# actually written into this Dockerfile — nothing here installed
# PhantomJS at all until this step.
#
# Resilient/soft-fail by design: this whole block runs in a subshell
# with its own `set -e`, and `|| echo "..."` on the outside means a
# failure ANYWHERE inside it (a mirror being down, the old-releases
# archive moving again, phantomjs failing its own smoke-test) can never
# fail the overall Docker build — every other feature of this API is
# fully independent of PhantomJS. phantomjs_available() at runtime is
# how a deployment finds out afterwards whether this step actually
# succeeded (GET /health).
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget ca-certificates bzip2 fontconfig ffmpeg \
        libfreetype6 libx11-6 libxext6 libxrender1 libjpeg62-turbo libpng16-16 \
    && rm -rf /var/lib/apt/lists/*

RUN ( set -e; \
      ( wget -q -O /tmp/phantomjs.tar.bz2 \
            https://github.com/Medium/phantomjs/releases/download/v2.1.1/phantomjs-2.1.1-linux-x86_64.tar.bz2 \
        || wget -q -O /tmp/phantomjs.tar.bz2 \
            https://bitbucket.org/ariya/phantomjs/downloads/phantomjs-2.1.1-linux-x86_64.tar.bz2 \
      ); \
      tar -xjf /tmp/phantomjs.tar.bz2 -C /tmp; \
      mv /tmp/phantomjs-2.1.1-linux-x86_64/bin/phantomjs /usr/local/bin/phantomjs.real; \
      chmod +x /usr/local/bin/phantomjs.real; \
      rm -rf /tmp/phantomjs*; \
      \
      # PhantomJS 2.1.1's prebuilt binary is linked against OpenSSL 1.0's
      # libssl.so.1.0.0/libcrypto.so.1.0.0, which this image's Debian 12
      # ("bookworm") base doesn't ship (OpenSSL 3 only) — without this,
      # the binary above fails immediately with "error while loading
      # shared libraries: libssl.so.1.0.0: cannot open shared object
      # file". Debian's own old-releases archive still serves the .deb.
      wget -q -O /tmp/libssl1.0.0.deb \
          http://archive.debian.org/debian/pool/main/o/openssl1.0/libssl1.0.0_1.0.2u-1~deb9u9_amd64.deb; \
      dpkg -i /tmp/libssl1.0.0.deb; \
      rm -f /tmp/libssl1.0.0.deb; \
      \
      # A PhantomJS-only OpenSSL config (activates the "legacy"
      # provider, which PhantomJS's ancient bundled WebKit TLS stack
      # needs against some sites) — scoped to ONLY the wrapper script
      # below via its own OPENSSL_CONF, never written to the system-wide
      # /etc/ssl/openssl.cnf, so this can't affect the modern TLS
      # behaviour python/curl_cffi/requests need everywhere else in this
      # app (see pornhub_resolver.py's own impersonate=chrome-124 logic,
      # which depends on that NOT being touched).
      mkdir -p /opt/phantomjs; \
      printf 'openssl_conf = openssl_init\n[openssl_init]\nproviders = provider_sect\n[provider_sect]\ndefault = default_sect\nlegacy = legacy_sect\n[default_sect]\nactivate = 1\n[legacy_sect]\nactivate = 1\n' \
          > /opt/phantomjs/openssl-legacy.cnf; \
      \
      # QT_QPA_PLATFORM=phantom: this PhantomJS build ships the Qt platform
      # plugin named "phantom" (not the generic Qt "offscreen" plugin).
      # Using "offscreen" causes the exact runtime failure:
      # "Could not find or load the Qt platform plugin \"offscreen\"".
      # The shipped "phantom" plugin is the headless platform intended
      # for this prebuilt PhantomJS binary.
      printf '#!/bin/sh\nexport QT_QPA_PLATFORM=phantom\nexport OPENSSL_CONF=/opt/phantomjs/openssl-legacy.cnf\nexec /usr/local/bin/phantomjs.real "$@"\n' \
          > /usr/local/bin/phantomjs; \
      chmod +x /usr/local/bin/phantomjs; \
      \
      # Smoke-test now, at build time, rather than only discovering a
      # broken install the first time a real request needs it — if this
      # fails, the `|| echo` below still lets the build finish, and
      # phantomjs_available() will correctly report False at runtime.
      /usr/local/bin/phantomjs --version; \
    ) || echo "[docker build] WARNING: PhantomJS setup failed/skipped — yt-dlp's PornHub JS-challenge fallback won't be available; every other PornHub extraction path (the common case) is unaffected. Check the lines above for which step failed."

COPY . .
RUN chmod +x entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
