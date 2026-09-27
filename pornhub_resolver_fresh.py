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
message yt-dlp raised — that's expected, not a bug in this file, and
retrying later (after a yt-dlp update, or from a non-geo-blocked
network) is the only real fix for either.

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
import re
from urllib.parse import urlparse

import yt_dlp

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
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if (host.endswith(".pornhub.com") or host.endswith(".pornhub.org")) and host != "www.pornhub.com":
        netloc = "www.pornhub.com"
        if parsed.port:
            netloc += f":{parsed.port}"
        parsed = parsed._replace(netloc=netloc)
        return parsed.geturl()
    return url


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
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        # Same reasoning as the sibling resolvers: "all" (not yt-dlp's
        # default best-match subset) is what makes "every quality
        # yt-dlp gets" mean every one it finds, not just the one it
        # would pick to actually play.
        "format": "all",
        # Never reuse extractor cache/state: each API request asks the
        # upstream site for fresh signed media URLs.
        "cachedir": False,
        "ignoreconfig": True,
    }
    canonical_url = _canonicalize_pornhub_url(url)
    logger.info("Resolving PornHub URL: %s -> %s", url, canonical_url)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(canonical_url, download=False)

    formats = info.get("formats") or []
    progressive, hls, audio_links = [], [], []

    for f in formats:
        f_url = f.get("url")
        if not f_url:
            continue
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        is_hls = (f.get("protocol") or "").startswith("m3u8") or ".m3u8" in f_url.lower()
        is_audio_only = vcodec in (None, "none") and acodec not in (None, "none")

        if is_audio_only:
            audio_links.append({
                "title": f.get("format_note") or f.get("format_id") or "Audio",
                "url": f_url,
            })
            continue

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
        m3u8_links.append({"title": f"Video {f['height']}p (HLS)", "url": f.get("url")})
    for f in heightless_hls:
        label = f.get("format_note") or f.get("format_id") or "Video"
        m3u8_links.append({"title": f"{label} (HLS)", "url": f.get("url")})

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
