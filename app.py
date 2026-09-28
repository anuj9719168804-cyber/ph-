"""Standalone PornHub-only resolver API."""
import logging
import time
import traceback
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
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
async def pornhub(url: str = Query(..., description="PornHub video link")):
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
    return {"status": True, "data": data, "credit": "Ak"}


@app.middleware("http")
async def rate_limit_mw(request, call_next):
    client_ip = request.client.host if request.client else "unknown"
    try:
        _check_rate_limit(client_ip)
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"status": False, "error": e.detail})
    return await call_next(request)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=config.PORT, reload=False)
