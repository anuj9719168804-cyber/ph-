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
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

import config
import hls_proxy
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
STREAM_MAX_ENTRIES = 2000
_stream_registry: dict = {}
_stream_lock = threading.Lock()
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _register_stream(cdn_url: str, name: str, proxy, kind: str = "mp4") -> str:
    """kind: "mp4" -> served by /stream/<code> (single-file pipe);
    "hls" -> served by /hls/<code>/master.m3u8 (rewriting playlist proxy).
    Each route only accepts its own kind, so a code for one can't be
    replayed against the other."""
    code = secrets.token_urlsafe(9)
    now = time.time()
    with _stream_lock:
        for k in [k for k, v in _stream_registry.items() if now - v["ts"] > STREAM_TTL]:
            del _stream_registry[k]
        while len(_stream_registry) >= STREAM_MAX_ENTRIES:
            del _stream_registry[min(_stream_registry, key=lambda k: _stream_registry[k]["ts"])]
        _stream_registry[code] = {"url": cdn_url, "name": name, "proxy": proxy, "ts": now, "kind": kind}
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
            "stream": "/stream/<code> (from each MP4 link's stream_url)",
            "hls": "/hls/<code>/master.m3u8 (from each m3u8 link's stream_url)",
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
        # False => yt-dlp's PhantomJS-dependent bot-challenge fallback
        # (see pornhub_resolver.py's phantomjs_available() docstring)
        # isn't available — the Dockerfile's own install step is best-
        # effort/soft-fail, so this can legitimately be False on a
        # deployment where every download mirror failed at build time.
        "phantomjs": pornhub_resolver.phantomjs_available(),
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
            raw_url = link["url"]
            code = _register_stream(raw_url, _safe_name(title, link.get("title")), proxy)
            # Expose only our stream URL; never return the raw CDN URL.
            link["stream_url"] = f"{base}/stream/{code}"
            link.pop("url", None)
            # Keep the public quality label compact: 240p, 480p, 720p, etc.
            link["title"] = link.get("title", "").replace("Video ", "", 1)
    # HLS quality links get a stream_url too. A raw master.m3u8 is only the
    # first hop -- every variant playlist / key / segment inside it is
    # ANOTHER IP-bound CDN URL, so the raw link can't be played from another
    # device either. /hls/<code>/master.m3u8 is a rewriting proxy: it fetches
    # each of those from this server's IP and points the player back here.
    for link in data.get("m3u8_links", []):
        if link.get("url"):
            raw_url = link["url"]
            code = _register_stream(raw_url, "hls", proxy, kind="hls")
            link["stream_url"] = f"{base}/hls/{code}/master.m3u8"
            link.pop("url", None)
    return {"status": True, "data": data, "credit": "Ak"}


@app.api_route("/stream/{code}", methods=["GET", "HEAD"], include_in_schema=False)
async def stream(code: str, request: Request):
    with _stream_lock:
        entry = _stream_registry.get(code)
    if not entry or entry.get("kind", "mp4") != "mp4" or time.time() - entry["ts"] > STREAM_TTL:
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


# ---------------------------------------------------------------------------
# HLS proxy (m3u8 quality links). See hls_proxy.py for the design notes.
#   /hls/<code>/master.m3u8      the quality's master/media playlist, rewritten
#   /hls/<code>/p/<token>[.ext]  any URL found inside a playlist (variant
#                                playlist, key, init segment, media segment);
#                                <token> is the upstream URL, base64url-encoded
# Playlists come back rewritten (text); everything else is piped through
# with Range pass-through. CORS is open so web players (hls.js etc.) work.
# ---------------------------------------------------------------------------
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Range, Content-Type",
    "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
}
_HLS_MIME = "application/vnd.apple.mpegurl"


def _hls_error(status: int, message: str):
    return JSONResponse(status_code=status, content={"status": False, "error": message}, headers=_CORS_HEADERS)


def _get_hls_entry(code: str):
    with _stream_lock:
        entry = _stream_registry.get(code)
    if not entry or entry.get("kind") != "hls" or time.time() - entry["ts"] > STREAM_TTL:
        return None
    return entry


async def _hls_serve(entry: dict, code: str, upstream_url: str, request: Request):
    # Never forward Range for playlists -- a partial playlist is useless.
    range_header = "" if hls_proxy.is_playlist_url(upstream_url) else request.headers.get("range", "")
    try:
        upstream, final_url = await run_in_threadpool(hls_proxy.open_upstream, entry, upstream_url, range_header)
    except hls_proxy.HostNotAllowed as e:
        logger.warning("HLS proxy refused host %s (code %s)", e, code)
        return _hls_error(403, f"Host not allowed for the HLS proxy: {e}")
    except Exception as e:
        logger.warning("HLS upstream fetch failed for %s: %s: %s", code, type(e).__name__, e)
        return _hls_error(502, f"Upstream fetch failed: {type(e).__name__}: {e}")

    if upstream.status_code >= 400:
        status_up = upstream.status_code
        upstream.close()
        return _hls_error(
            502,
            f"CDN returned HTTP {status_up} (link expired, or resolved from a different IP than this server now uses).",
        )

    content_type = upstream.headers.get("Content-Type", "")

    if hls_proxy.looks_like_playlist(final_url, content_type):
        try:
            raw = await run_in_threadpool(hls_proxy.read_capped, upstream)
        except Exception as e:
            return _hls_error(502, f"Could not read playlist: {type(e).__name__}: {e}")
        finally:
            upstream.close()
        text = raw.decode("utf-8", errors="replace")
        if not text.lstrip("\ufeff \t\r\n").startswith("#EXTM3U"):
            return _hls_error(502, "Upstream did not return a valid HLS playlist.")
        headers = {"Cache-Control": "no-cache", **_CORS_HEADERS}
        if request.method == "HEAD":
            return Response(status_code=200, headers=headers, media_type=_HLS_MIME)
        body = hls_proxy.rewrite_playlist(text.lstrip("\ufeff"), final_url, code)
        return Response(content=body, media_type=_HLS_MIME, headers=headers)

    # Media segment / key / init section: pipe it through.
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache", **_CORS_HEADERS}
    if upstream.headers.get("Content-Range"):
        headers["Content-Range"] = upstream.headers["Content-Range"]
    # Content-Length only when the body isn't content-encoded (requests
    # transparently decodes gzip, which would make the length wrong).
    if upstream.headers.get("Content-Length") and not upstream.headers.get("Content-Encoding"):
        headers["Content-Length"] = upstream.headers["Content-Length"]
    media_type = content_type or "application/octet-stream"
    if request.method == "HEAD":
        status_up = upstream.status_code
        upstream.close()
        return StreamingResponse(iter(()), status_code=status_up, headers=headers, media_type=media_type)

    def body():
        try:
            for chunk in upstream.iter_content(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return StreamingResponse(body(), status_code=upstream.status_code, headers=headers, media_type=media_type)


@app.api_route("/hls/{code}/master.m3u8", methods=["GET", "HEAD"], include_in_schema=False)
async def hls_master(code: str, request: Request):
    entry = _get_hls_entry(code)
    if not entry:
        return _hls_error(404, "Stream not found or expired.")
    return await _hls_serve(entry, code, entry["url"], request)


@app.api_route("/hls/{code}/p/{token}", methods=["GET", "HEAD"], include_in_schema=False)
async def hls_part(code: str, token: str, request: Request):
    entry = _get_hls_entry(code)
    if not entry:
        return _hls_error(404, "Stream not found or expired.")
    try:
        upstream_url = hls_proxy.token_to_url(token)
    except Exception:
        return _hls_error(400, "Malformed HLS token.")
    return await _hls_serve(entry, code, upstream_url, request)


@app.options("/hls/{path:path}", include_in_schema=False)
async def hls_options(path: str):
    return Response(status_code=204, headers=_CORS_HEADERS)


@app.middleware("http")
async def rate_limit_mw(request, call_next):
    if request.url.path.startswith(("/stream/", "/hls/")):
        return await call_next(request)  # players issue many Range / segment requests
    client_ip = request.client.host if request.client else "unknown"
    try:
        _check_rate_limit(client_ip)
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"status": False, "error": e.detail})
    return await call_next(request)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=config.PORT, reload=False)
