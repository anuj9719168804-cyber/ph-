#!/bin/sh
set -e

# Tor's own SOCKS5 proxy -- see Dockerfile's comment and
# pornhub_resolver.py's PORNHUB_DISABLE_TOR / retry-ladder handling.
# Started here (not in the Python process) so it's up before the first
# request can possibly hit it, and so a Tor crash doesn't take the API
# down with it (uvicorn keeps running either way; requests just fall
# back to failing the geo-block with a clear error, same as before Tor
# existed here, instead of hanging on a dead proxy).
TOR_LOG=/var/log/tor-bootstrap.log
mkdir -p "$(dirname "$TOR_LOG")"
echo "[entrypoint] starting Tor (free SOCKS5 proxy on 127.0.0.1:9050)..."
tor --SocksPort 9050 --Log "notice file $TOR_LOG" --DataDirectory /var/lib/tor >/dev/null 2>&1 &

# Tor typically bootstraps in 5-30s (building its first circuits); wait
# up to 60s so the FIRST real request after a cold deploy doesn't race
# it the same way the earlier FlareSolverr cold-start race did in the
# sibling Diskwala project. This is a one-time startup cost, not paid
# per-request -- pornhub_resolver.py only reaches for Tor at all when a
# request actually hits the geo-block.
TOR_TIMEOUT=60
elapsed=0
while [ "$elapsed" -lt "$TOR_TIMEOUT" ]; do
    if grep -q "Bootstrapped 100%" "$TOR_LOG" 2>/dev/null; then
        echo "[entrypoint] Tor is up on 127.0.0.1:9050"
        break
    fi
    sleep 2
    elapsed=$((elapsed + 2))
done
if [ "$elapsed" -ge "$TOR_TIMEOUT" ]; then
    echo "[entrypoint] WARNING -- Tor did not finish bootstrapping after ${TOR_TIMEOUT}s (see $TOR_LOG)."
    echo "[entrypoint] the API will still start; the PornHub geo-block workaround just won't be ready yet."
fi

echo "[entrypoint] API started -- binding \$PORT..."
exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}"
