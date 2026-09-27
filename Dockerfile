FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Tor gives this container its own free SOCKS5 proxy (127.0.0.1:9050),
# used by pornhub_resolver.py's geo-block retry ladder as a last-resort
# fallback -- no manual PORNHUB_PROXY/PORNHUB_PROXIES or cookies file
# needed for the age-verification-law geo-block to be worked around.
RUN apt-get update && apt-get install -y --no-install-recommends tor \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .
RUN chmod +x entrypoint.sh

EXPOSE 8000

# entrypoint.sh starts Tor (and waits for it to finish bootstrapping)
# before handing off to uvicorn -- see that file for details.
ENTRYPOINT ["./entrypoint.sh"]
