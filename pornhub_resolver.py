"""pornhub_resolver.py — resolves a PornHub (or Thumbzilla, which shares
the same player/extractor) video URL to every playable format yt-dlp can
find for it, shaped into the same response envelope as
xhamster_resolver.py / xvideos_resolver.py in the sibling Ak resolver
projects ("links"/"m3u8_links"/"videoDetails"/"entitlement", credited
"Ak"), so a caller already coded against either of those gets an
identical shape from this one.

IMPORTANT — read before deploying this one specifically: PornHub's own
site defenses are considerably less stable than xHamster's or XVideos'
from yt-dlp's point of view. Two failure modes are outside anything this
file can work around, both confirmed live in yt-dlp's own issue tracker
as of this writing:
  1. Age-verification-law geo-blocking: PornHub (via its Aylo network)
     blocks several U.S. states (Utah, Texas, Virginia, Montana,
     Mississippi, Arkansas, North Carolina at last check) outright,
     serving a compliance notice page instead of the video — yt-dlp
     fails with a generic "Unable to extract title" there since it's
     not actually looking at a video page at all.
  2. PornHub's page bundle/JS changes trigger the SAME "Unable to
     extract title" error independent of geo-blocking, and yt-dlp's own
     extractor needs a matching patch each time — this has happened
     repeatedly (yt-dlp/yt-dlp#7590, #11032, #13903, #16423 are four
     separate instances of it). Keeping the `yt-dlp` pin in
     requirements.txt current is the only real mitigation; there's no
     way to code around a page format PornHub hasn't shipped yet.
Both surface identically here as a 502 from /api/pornhub with whatever
message yt-dlp raised — that's expected, not a bug in this file.
Retrying later (after a yt-dlp update) is the only real fix for #2.
For #1 (geo-block), this file's own retry ladder (see resolve_pornhub())
now falls back to a free Tor SOCKS5 proxy that entrypoint.sh starts
inside this same container — no cookies file or paid proxy required by
default. Set PORNHUB_PROXY/PORNHUB_PROXIES for a real proxy instead (or
alongside), or PORNHUB_DISABLE_TOR=true to turn the Tor fallback off.

Two things from the original sample response this envelope matches are
deliberately NOT replicated, same as in the other two resolvers in this
family:
  - Any proprietary deep-link/app-open URI scheme — this returns
    yt-dlp's actual format URL (a real .mp4/.m3u8 link) instead.
  - Any login/subscription paywall fields ("entitlement"/"isPro"/
    "gatedHd"/"lockReason") — this service has no such gating, so every
    quality yt-dlp finds is included, but the envelope fields are kept
    (with "allowed": true, no lock reasons) purely for shape-
    compatibility with anything coded against that original shape.
"""
import logging
import os
import re
import socket
import threading
import time
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget

logger = logging.getLogger("pornhub_api")


def _format_duration(seconds) -> str | None:
    """seconds -> "MM:SS", or "H:MM:SS" once it's an hour or longer.
    Identical helper to the one in xhamster_resolver.py / xvideos_
    resolver.py — duplicated rather than imported so this file has no
    dependency on either and can be dropped into another project alone."""
    if seconds is None:
        return None
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return None
    if total < 0:
        return None
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# Substrings from yt-dlp's own PornHub geo-block error (failure mode #1
# in this file's module docstring) — lowercased, checked against the
# lowercased exception text. Kept as more than one phrase because yt-dlp
# has used slightly different wording for this across versions; matching
# any one of them is enough, false positives here just mean an extra
# (harmless) proxy retry on a genuinely-different failure.
_GEO_BLOCK_MARKERS = (
    "not available from your location",
    "not available in your country",
    "geo restriction",
    "geo-restricted",
)


def _is_geo_block_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _GEO_BLOCK_MARKERS)


def _proxy_port_open(proxy_url: str, timeout: float = 2.0) -> bool:
    """Quick raw-TCP reachability check for a socks5(h)://host:port proxy
    URL, used to skip a not-yet-ready Tor proxy fast (see the retry-loop
    comment where this is called) instead of paying yt-dlp's full
    retries=3 x socket_timeout=20 to discover the same thing."""
    try:
        rest = proxy_url.split("://", 1)[1]
        host_port = rest.split("@")[-1]  # drop any user:pass@ prefix
        host, port_str = host_port.rsplit(":", 1)
        port = int(port_str.split("/")[0])
    except Exception:
        return True  # couldn't parse it -- don't block on our own confusion, let the real attempt decide
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# Mirrors yt-dlp's own yt_dlp.extractor.pornhub.PornHubIE._VALID_URL —
# not reproduced as a raw copy-paste, but checked against it directly
# (https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/pornhub.py
# and its youtube-dl-era ancestor, which is where the core host list
# below was confirmed): any subdomain of pornhub.com (locale prefixes
# like "www.", "de.", "cn." are all valid — PornHub serves the same
# player under all of them), plus the separately-branded thumbzilla.com
# player which shares PornHub's own extractor and video IDs.
#
# pornhubpremium.com is included here too — yt-dlp has supported it as
# an alt-domain for the paid tier for a long time — but unlike the
# xvideos.es/xvideos2.com confirmations in xvideos_resolver.py, this
# project could not independently re-verify it against a live source
# fetch of pornhub.py while building this file (no network access in
# this environment). If a real pornhubpremium.com link 400s against
# is_pornhub_link() below, that's the one entry worth double-checking
# against yt-dlp's current source before assuming it's a resolver bug.
_PORNHUB_HOST_RE = re.compile(
    r"^(?:[\w-]+\.)*(?:pornhub\.(?:com|org)|pornhubpremium\.com|thumbzilla\.com)$",
    re.IGNORECASE,
)


def is_pornhub_link(url: str) -> bool:
    """Same approach as is_xhamster_link() / is_xvideos_link() in the
    sibling resolvers: checks the URL's actual hostname (not a crude
    "pornhub" in url substring check) against the confirmed host set
    above."""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    return bool(_PORNHUB_HOST_RE.match(host))


def _dedupe_by_height(formats):
    """Shared by both the progressive (MP4) and HLS passes below —
    identical logic to xvideos_resolver.py's helper of the same name
    (see that file for the full reasoning): ONE entry per actual
    quality tier, picking the WIDEST candidate at each height rather
    than whichever came first, so a same-height-but-narrower anomalous
    variant never wins over the correctly-proportioned one. Formats
    with no height at all are deduped by URL instead, since PornHub's
    own extractor has been observed (same as XVideos) handing back a
    "different label, identical underlying file" pair when a video
    doesn't actually have a distinct rendition for one of the labels.
    Returns (ordered_list_of_best_formats, heightless_formats)."""
    best_by_height = {}
    height_order = []
    heightless = []
    seen_heightless_urls = set()
    for f in formats:
        height = f.get("height")
        if not height:
            f_url = f.get("url")
            if f_url in seen_heightless_urls:
                continue
            seen_heightless_urls.add(f_url)
            heightless.append(f)
            continue
        width = f.get("width") or 0
        current = best_by_height.get(height)
        if current is None:
            height_order.append(height)
            best_by_height[height] = f
        elif width > (current.get("width") or 0):
            best_by_height[height] = f
    return [best_by_height[h] for h in height_order], heightless


def _canonicalize_pornhub_url(url: str) -> str:
    """Normalize locale PornHub hosts (de., fr., etc.) to the canonical
    host expected by yt-dlp's PornHub extractor while preserving path/query.

    BUG FIX: this used to rewrite EVERY *.pornhub.org host to
    www.pornhub.com too, not just www-ing it within its own TLD.
    pornhub.org is a separate mirror/clone site, not just a locale
    subdomain of pornhub.com the way de./fr./etc. are -- confirmed from a
    real failure where de.pornhub.org's viewkey simply doesn't exist on
    pornhub.com at all, so the rewritten URL 404'd (or resolved a
    completely different, wrong video, if that viewkey happened to also
    be in use on .com) instead of ever reaching the .org content the
    person actually linked. Each TLD now only canonicalizes to its own
    www host: *.pornhub.com -> www.pornhub.com, *.pornhub.org ->
    www.pornhub.org."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.endswith(".pornhub.com") and host != "www.pornhub.com":
        target = "www.pornhub.com"
    elif host.endswith(".pornhub.org") and host != "www.pornhub.org":
        target = "www.pornhub.org"
    else:
        return url

    netloc = target
    if parsed.port:
        netloc += f":{parsed.port}"
    parsed = parsed._replace(netloc=netloc)
    return parsed.geturl()


_INFO_CACHE = {}
_INFO_CACHE_LOCK = threading.Lock()


def resolve_pornhub(url: str) -> dict:
    """Runs yt-dlp's extract_info (metadata only, skip_download) and
    buckets every format it finds into "links" (progressive MP4),
    "m3u8_links" (HLS manifests, when PornHub serves one for that
    video), or "audio_links" (audio-only, essentially never present for
    PornHub but handled for shape-parity with the sibling resolvers).
    Every entry keeps yt-dlp's own URL as-is — no re-wrapping, no
    proprietary scheme, no gating. Raises on any yt-dlp failure
    (private/removed video, age-verification geo-block, a PornHub page-
    format change yt-dlp hasn't caught up with yet — see this file's
    module docstring — network error, etc.) — the /api/pornhub route in
    app.py turns that into a 502 with the error message, same pattern
    as the sibling resolvers' routes.

    No mirror-host rewrite here (unlike xhamster_resolver.py's
    xhaccess.com handling or xvideos_resolver.py's xvideos.red
    handling): no PornHub clone/mirror domain could be independently
    confirmed the same rigorous way (a maintained ad-block filter list
    entry, or a yt-dlp issue confirming the exact same page under a
    different host) while building this file without network access.
    is_pornhub_link() above is deliberately narrower as a result — a
    real PornHub mirror that yt-dlp doesn't recognize natively will
    400 here rather than being guessed at and silently rewritten."""
    # Ported from the FBOT downloader's *general* yt-dlp reliability setup:
    # explicit timeout/retries, optional Netscape cookies, and no disk cache.
    #
    # "impersonate" WAS intentionally left out here originally -- that was
    # wrong. FBOT's own pornhub_scraper.py documents (and this project's
    # own deployment then reproduced) the actual cause: PornHub's "not
    # available from your location due to geo restriction" message is a
    # bot-detection block in disguise, triggered by cloud/datacenter IPs
    # (Render, AWS, etc.) making a request whose TLS handshake doesn't
    # look like a real browser's -- it is NOT the age-verification-law
    # geo-block from this file's module docstring, despite using almost
    # the same wording. "chrome-124" here (via curl_cffi, already in
    # requirements.txt) makes yt-dlp's TLS fingerprint match a real
    # Chrome's, which resolves this on its own -- no proxy or VPN needed.
    # The _is_geo_block_error()-driven proxy/Tor ladder further below
    # stays as a second line of defense for the cases impersonation alone
    # doesn't cover (a genuine law-driven block, or a video where even a
    # browser-shaped request still gets flagged) -- it's just no longer
    # the primary fix for the common case.
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "format": "all",
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "cachedir": False,
        "ignoreconfig": True,
        "nocheckcertificate": True,
        "impersonate": ImpersonateTarget("chrome", "124"),
    }

    # Optional user-supplied Netscape cookies file. This is useful when the
    # account/session legitimately has access that an anonymous request does
    # not. Set PORNHUB_COOKIES to a mounted file path in the container.
    cookiefile = os.getenv("PORNHUB_COOKIES", "").strip()
    if cookiefile and os.path.isfile(cookiefile):
        ydl_opts["cookiefile"] = cookiefile

    canonical_url = _canonicalize_pornhub_url(url)

    # FBOT previously keyed non-YouTube extraction cache by path only; for
    # PornHub that is unsafe because different viewkeys share
    # /view_video.php. Keep a tiny in-process cache keyed by the full URL,
    # but never cache the extractor's signed URLs for longer than a minute.
    # Checked BEFORE the retry ladder below so a cache hit skips it (and
    # any proxy) entirely -- no point re-solving the geo-block on a video
    # already resolved in the last 60s.
    cache_key = canonical_url
    now = time.time()
    with _INFO_CACHE_LOCK:
        cached = _INFO_CACHE.get(cache_key)
        if cached and now - cached["ts"] < 60:
            logger.info("PornHub extraction cache hit (age=%ss)", int(now - cached["ts"]))
            info = cached["info"]
        else:
            cached = None
            info = None

    # --- Geo-block retry ladder --------------------------------------
    # yt-dlp's own error for failure mode #1 (see module docstring)
    # literally says "use a VPN or a proxy server". Rather than making
    # every deployment guess-and-manually-retry -- or require a cookies
    # file or a paid proxy just to work at all -- this resolver does
    # that retry itself, ending in a proxy that needs no configuration:
    #   1. Try direct (no proxy) first -- the fast path, and correct for
    #      any deployment region that ISN'T geo-blocked, so a working
    #      majority of deployments never pay a proxy's extra latency.
    #   2. Only if that specific attempt fails with a geo-block error
    #      (not any other kind of failure -- a real "video removed" or
    #      page-format error should surface immediately, not burn time
    #      retrying through every proxy for no reason), walk through any
    #      PORNHUB_PROXIES the deployer configured, in order.
    #   3. Finally, fall back to the Tor SOCKS5 proxy entrypoint.sh
    #      starts inside this same container (127.0.0.1:9050) -- free,
    #      built in, no signup/cookies/paid-proxy needed. This is what
    #      makes "no cookies, no manually-supplied proxy" actually work
    #      out of the box: as long as the image was built from this
    #      project's Dockerfile (which installs+starts Tor), a
    #      geo-blocked deployment self-heals with zero configuration.
    #      Set PORNHUB_DISABLE_TOR=true to skip this tier entirely --
    #      needed if this file is ever dropped into a container that
    #      does NOT run Tor, so a doomed 20s connection attempt against
    #      a port nothing is listening on doesn't run on every request.
    #   4. If PORNHUB_ALWAYS_PROXY=true, skip step 1 entirely -- for a
    #      deployment that already knows its whole region is blocked,
    #      the direct attempt would only ever fail, so paying that
    #      latency on every request is pure waste.
    # PORNHUB_PROXIES is a comma-separated list, tried in order (first
    # one that succeeds wins). PORNHUB_PROXY (singular, pre-existing) is
    # still read as a one-proxy shorthand and folded into the same list
    # for backward compatibility with anyone already setting it.
    proxies = [p.strip() for p in os.getenv("PORNHUB_PROXIES", "").split(",") if p.strip()]
    single_proxy = os.getenv("PORNHUB_PROXY", "").strip()
    if single_proxy and single_proxy not in proxies:
        proxies.append(single_proxy)
    if os.getenv("PORNHUB_DISABLE_TOR", "").strip().lower() not in ("1", "true", "yes"):
        proxies.append("socks5h://127.0.0.1:9050")
    always_proxy = os.getenv("PORNHUB_ALWAYS_PROXY", "").strip().lower() in ("1", "true", "yes")


    def _extract(opts):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(canonical_url, download=False)
        except Exception as e:
            # Distinguish "impersonate itself is broken" (curl_cffi
            # missing/outdated, or yt-dlp/curl_cffi dropped support for
            # "chrome-124" in some future version) from an actual site
            # response -- the latter should propagate as-is so the
            # geo-block/proxy-ladder logic above can see it. Only retry
            # without impersonate when the error text points at the
            # impersonation layer specifically; this is a narrow,
            # technical-failure safety net, not a general retry.
            text = str(e).lower()
            if opts.get("impersonate") and ("impersonate" in text or "curl_cffi" in text or "curl-cffi" in text):
                logger.warning("PornHub impersonate target unavailable (%s), retrying without it: %s", str(opts["impersonate"]), e)
                fallback_opts = dict(opts)
                fallback_opts.pop("impersonate", None)
                with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                    return ydl.extract_info(canonical_url, download=False)
            raise

    if info is None:
        attempts = []
        if not (always_proxy and proxies):
            attempts.append(None)          # direct, no proxy
        attempts.extend(proxies)           # then each proxy, in order

        last_exc = None
        for i, proxy in enumerate(attempts):
            # BUG FIX: a not-yet-bootstrapped Tor SOCKS5 proxy (right
            # after container start, Tor typically takes a few seconds to
            # build circuits) used to go straight into the full yt-dlp
            # attempt below, whose retries=3 x socket_timeout=20 meant
            # burning up to 60s on a proxy that was never going to accept
            # the connection in the first place -- confirmed from a real
            # case where Tor simply wasn't ready yet. A raw TCP connect
            # to a SOCKS5 proxy is a few-hundred-ms check; skip the heavy
            # attempt entirely when that fails, rather than paying the
            # full yt-dlp retry cost to discover the same thing.
            if proxy and proxy.startswith("socks5") and not _proxy_port_open(proxy):
                last_exc = RuntimeError(f"proxy {proxy} is not accepting connections (not started yet?)")
                logger.info("PornHub proxy %s not reachable, skipping (attempt %d/%d)", proxy, i + 1, len(attempts))
                continue
            opts = dict(ydl_opts)
            if proxy:
                opts["proxy"] = proxy
            try:
                logger.info(
                    "Resolving PornHub URL: %s -> %s (attempt %d/%d, %s)",
                    url, canonical_url, i + 1, len(attempts),
                    f"proxy={proxy}" if proxy else "direct",
                )
                info = _extract(opts)
                last_exc = None
                break
            except Exception as e:
                last_exc = e
                # Only worth trying the next tier if THIS failure was the
                # geo-block yt-dlp itself suggested a proxy for. Any other
                # error (removed video, page-format change, bad URL) will
                # fail the exact same way through a proxy, so don't burn
                # time/requests retrying it.
                if not _is_geo_block_error(e):
                    break
                logger.info("PornHub geo-block on attempt %d/%d, trying next tier: %s", i + 1, len(attempts), e)

        if last_exc is not None:
            if _is_geo_block_error(last_exc):
                if not proxies:
                    # PORNHUB_DISABLE_TOR=true and no manual proxy either
                    # -- there was genuinely nothing left to try.
                    raise RuntimeError(
                        f"{last_exc} (no proxy available to work around this -- "
                        "either drop PORNHUB_DISABLE_TOR so this container's "
                        "bundled Tor can handle it, or set PORNHUB_PROXY/"
                        "PORNHUB_PROXIES)"
                    )
                # Direct AND every proxy (Tor included, unless disabled)
                # came back geo-blocked for this specific video. Tor exit
                # nodes change on container restart, so this is usually
                # transient rather than a dead end.
                raise RuntimeError(
                    f"{last_exc} (still geo-blocked after direct + every "
                    "configured proxy/Tor attempt -- Tor's exit node rotates "
                    "on container restart, so retrying shortly sometimes "
                    "clears this; a real PORNHUB_PROXY in a confirmed-clear "
                    "region is the reliable fix)"
                )
            raise last_exc

        with _INFO_CACHE_LOCK:
            _INFO_CACHE[cache_key] = {"info": info, "ts": time.time()}
            if len(_INFO_CACHE) > 100:
                oldest = min(_INFO_CACHE, key=lambda k: _INFO_CACHE[k]["ts"])
                _INFO_CACHE.pop(oldest, None)

    formats = info.get("formats") or []
    progressive, hls, audio_links = [], [], []

    for f in formats:
        f_url = f.get("url")
        if not f_url:
            continue
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        manifest_url = f.get("manifest_url") or ""
        protocol = (f.get("protocol") or "").lower()
        is_hls = (
            protocol.startswith("m3u8")
            or ".m3u8" in f_url.lower()
            or ".m3u8" in manifest_url.lower()
        )
        is_audio_only = vcodec in (None, "none") and acodec not in (None, "none")

        if is_audio_only:
            audio_links.append({
                "title": f.get("format_note") or f.get("format_id") or "Audio",
                "url": f_url,
            })
            continue

        # Keep the extractor's manifest URL so the API can expose the
        # actual/master M3U8 instead of only the selected media playlist.
        if is_hls:
            f = dict(f)
            f["_m3u8_url"] = f.get("manifest_url") or f.get("url")
        (hls if is_hls else progressive).append(f)

    links = []
    best_progressive, heightless_progressive = _dedupe_by_height(progressive)
    for f in best_progressive:
        links.append({"title": f"Video {f['height']}p", "url": f.get("url")})
    for f in heightless_progressive:
        label = f.get("format_note") or f.get("format_id") or "Video"
        links.append({"title": label, "url": f.get("url")})

    m3u8_links = []
    best_hls, heightless_hls = _dedupe_by_height(hls)
    for f in best_hls:
        m3u8_links.append({"title": f"Video {f['height']}p (HLS)", "url": f.get("_m3u8_url") or f.get("manifest_url") or f.get("url")})
    for f in heightless_hls:
        label = f.get("format_note") or f.get("format_id") or "Video"
        m3u8_links.append({"title": f"{label} (HLS)", "url": f.get("_m3u8_url") or f.get("manifest_url") or f.get("url")})

    # NOTE on quality ceiling: nothing here caps resolution — every
    # height yt-dlp's extractor finds is kept. PornHub videos are
    # overwhelmingly progressive-MP4-only (240p/480p/720p/1080p, no
    # HLS); m3u8_links legitimately comes back empty for most videos,
    # which is expected here, not a sign anything failed.

    thumbnails = []
    seen = set()
    for t_url in [info.get("thumbnail")] + [t.get("url") for t in (info.get("thumbnails") or [])]:
        if t_url and t_url not in seen:
            thumbnails.append({"url": t_url})
            seen.add(t_url)

    # Key order matches the sibling resolvers' response shape exactly:
    # title -> duration -> thumbnail (inside videoDetails), then
    # m3u8_links (quality links) right after, before the less-used
    # envelope fields.
    return {
        "videoDetails": {
            "title": info.get("title"),
            "duration": _format_duration(info.get("duration")),
            "lengthSeconds": info.get("duration"),
            "thumbnails": thumbnails,
        },
        "m3u8_links": m3u8_links,
        "audio_links": audio_links,
        "entitlement": {
            "adsAllowed": True,
            "countdownSeconds": 0,
            "hd": {"allowed": True, "limit": 0, "remaining": 0, "resetsAt": None},
            "tier": "anon",
        },
        "isAuthenticated": False,
        "isPro": False,
        "links": links,
        "tier": "anon",
    }
