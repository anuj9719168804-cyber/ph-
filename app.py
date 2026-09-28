"""Standalone PornHub-only resolver API."""
import logging
import os
import re
import secrets
import threading
import time
import traceback
from collections import defaultdict, deque

import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

import config
import pornhub_resolver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("pornhub_api")

app = FastAPI(
    title="PornHub Resolver API",
    description="Standalone PornHub-only video resolver API.",
    version="1.0.0",
)

# ---------------------------------------------------------------------------
# Stream proxy (ported from FBOT's keep_alive.py /stream/<code> proxy).
# PornHub's phncdn.com links are signed AND bound to the IP that resolved them
# (this server's, or the proxy's). Opening one from a phone/browser on another
# IP gives the CDN's "Page not found". So each MP4 link also gets a
# /stream/<code> URL that this server fetches itself (same IP) and pipes back,
# with Range pass-through so seeking works. Codes are random and expire.
# ---------------------------------------------------------------------------
STREAM_TTL = 3600
STREAM_MAX_ENTRIES = 500
_stream_registry: dict = {}
_stream_lock = threading.Lock()
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _register_stream(cdn_url: str, name: str, proxy) -> str:
    code = secrets.token_urlsafe(9)
    now = time.time()
    with _stream_lock:
        for k in [k for k, v in _stream_registry.items() if now - v["ts"] > STREAM_TTL]:
            del _stream_registry[k]
        while len(_stream_registry) >= STREAM_MAX_ENTRIES:
            del _stream_registry[min(_stream_registry, key=lambda k: _stream_registry[k]["ts"])]
        _stream_registry[code] = {"url": cdn_url, "name": name, "proxy": proxy, "ts": now}
    return code


def _public_base(request: Request) -> str:
    explicit = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip().strip("/")
    if host:
        return f"https://{host}"
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{proto}://{request.headers.get('x-forwarded-host', request.headers.get('host', ''))}"


def _safe_name(title, label) -> str:
    base = re.sub(r"[^\w\-. ]+", "", f"{title or 'video'} {label or ''}").strip() or "video"
    return base[:120] + ".mp4"


def _open_upstream(entry: dict, range_header: str):
    headers = {"User-Agent": _UA, "Referer": "https://www.pornhub.com/"}
    if range_header:
        headers["Range"] = range_header
    proxies = {"http": entry["proxy"], "https": entry["proxy"]} if entry.get("proxy") else None
    return requests.get(
        entry["url"], headers=headers, stream=True, timeout=(10, 300),
        allow_redirects=True, proxies=proxies,
    )


_hits: dict[str, deque] = defaultdict(deque)
EXAMPLE_URL = "https://www.pornhub.com/view_video.php?viewkey=000000000000000"


def _check_rate_limit(client_ip: str):
    now = time.time()
    window = _hits[client_ip]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= config.RATE_LIMIT_PER_MINUTE:
        raise HTTPException(status_code=429, detail="Rate limit exceeded, try again in a bit.")
    window.append(now)


@app.get("/", include_in_schema=False)
def root():
    return {
        "status": True,
        "creator": "PornHub API",
        "message": "PornHub Resolver API is online",
        "version": "1.0.0",
        "example": {
            "url": EXAMPLE_URL,
            "resolve": f"/api/pornhub?url={EXAMPLE_URL}",
        },
        "endpoints": {
            "resolve": "/api/pornhub?url=<PORNHUB_VIDEO_URL>",
            "stream": "/stream/<code> (from each link's stream_url)",
            "health": "/health",
        },
    }


@app.get("/health")
def health():
    return {
        "status": True,
        "service": "pornhub-api",
        "provider": "PornHub",
        # False => yt-dlp can't use curl_cffi here (unsupported curl_cffi version?)
        "impersonation": pornhub_resolver.impersonation_available(),
    }


@app.get("/api/pornhub")
async def pornhub(request: Request, url: str = Query(..., description="PornHub video link")):
    if not pornhub_resolver.is_pornhub_link(url):
        return JSONResponse(
            status_code=400,
            content={"status": False, "error": "Only PornHub links are supported here."},
        )
    try:
        data = await run_in_threadpool(pornhub_resolver.resolve_pornhub, url)
    except Exception as e:
        # BUG FIX: str(e) can be empty for some exception types (e.g. a
        # bare-raised exception with no message, or some urllib3/requests
        # errors whose __str__ returns ""), which used to produce a
        # useless "PornHub resolve failed for ...: " log line with
        # nothing after the colon and an equally empty {"error": ""} in
        # the response -- confirmed from a real log showing exactly that.
        # Fall back to the exception's type name (and repr as a second
        # fallback) so there's always something to actually debug from.
        detail = str(e) or repr(e) or "unknown error"
        # BUG FIX: an AssertionError (or similar bare exception) has no
        # useful str()/repr() at all -- confirmed from a real log showing
        # exactly "[AssertionError] AssertionError()" and nothing else,
        # which says WHAT failed but not WHERE inside yt-dlp's extractor
        # it happened. The full traceback is what actually pinpoints that;
        # logging it here (server-side only, never in the API response)
        # costs nothing on the happy path and is the difference between
        # guessing and knowing the next time this kind of bare exception
        # comes up.
        logger.warning(
            "PornHub resolve failed for %s: [%s] %s\n%s",
            url, type(e).__name__, detail, traceback.format_exc(),
        )
        return JSONResponse(status_code=502, content={"status": False, "error": f"{type(e).__name__}: {detail}"})
    proxy = data.pop("_proxy", None)
    base = _public_base(request)
    title = (data.get("videoDetails") or {}).get("title")
    for link in data.get("links", []):
        if link.get("url"):
            code = _register_stream(link["url"], _safe_name(title, link.get("title")), proxy)
            # "url" stays the raw CDN link (IP-bound); "stream_url" works from any device.
            link["stream_url"] = f"{base}/stream/{code}"
    return {"status": True, "data": data, "credit": "Ak"}


@app.api_route("/stream/{code}", methods=["GET", "HEAD"], include_in_schema=False)
async def stream(code: str, request: Request):
    with _stream_lock:
        entry = _stream_registry.get(code)
    if not entry or time.time() - entry["ts"] > STREAM_TTL:
        return JSONResponse(status_code=404, content={"status": False, "error": "Stream not found or expired."})
    try:
        upstream = await run_in_threadpool(_open_upstream, entry, request.headers.get("range", ""))
    except Exception as e:
        logger.warning("Stream fetch failed for %s: %s", code, e)
        return JSONResponse(status_code=502, content={"status": False, "error": f"Upstream fetch failed: {type(e).__name__}: {e}"})
    if upstream.status_code >= 400:
        code_up = upstream.status_code
        upstream.close()
        return JSONResponse(status_code=502, content={
            "status": False,
            "error": f"CDN returned HTTP {code_up} (link expired, or resolved from a different IP than this server now uses).",
        })
    headers = {
        "Content-Disposition": f'inline; filename="{entry["name"]}"',
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-cache",
    }
    for h in ("Content-Length", "Content-Range"):
        if upstream.headers.get(h):
            headers[h] = upstream.headers[h]
    media_type = upstream.headers.get("Content-Type", "video/mp4")
    if request.method == "HEAD":
        upstream.close()
        return StreamingResponse(iter(()), status_code=upstream.status_code, headers=headers, media_type=media_type)

    def body():
        try:
            for chunk in upstream.iter_content(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return StreamingResponse(body(), status_code=upstream.status_code, headers=headers, media_type=media_type)


@app.middleware("http")
async def rate_limit_mw(request, call_next):
    if request.url.path.startswith("/stream/"):
        return await call_next(request)  # players issue many Range requests
    client_ip = request.client.host if request.client else "unknown"
    try:
        _check_rate_limit(client_ip)
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"status": False, "error": e.detail})
    return await call_next(request)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=config.PORT, reload=False)
