# 🚀 PornHub Resolver API

Standalone **PornHub-only** REST API built with FastAPI and yt-dlp, following the simple structure of the Ak resolver projects (same shape as the sibling `xhamster-api` and `xvideos-api`).

## What it supports

- PornHub and Thumbzilla video URLs
- Metadata returned by yt-dlp
- Progressive/direct video formats (240p/480p/720p/1080p MP4, depending on the upload)
- HLS/m3u8 formats on the (uncommon) videos where PornHub serves one
- Audio-only formats when available
- Thumbnail information
- Per-IP rate limiting
- `/health` health check
- Docker deployment
- Render / Railway / VPS / Docker-compatible hosting

It does **not** include Diskwala, xHamster, XVideos, YouTube, Instagram, Telegram sessions, Chromium, Node.js, Cloudflare bypass, or FlareSolverr.

## ⚠️ Before you deploy this one

PornHub is meaningfully less stable to resolve than xHamster/XVideos from yt-dlp's side, for two reasons outside this project's control:

1. **Age-verification-law geo-blocking.** PornHub's network (Aylo) blocks several U.S. states outright and serves a compliance page instead of the video from an IP in one of them — this comes back as a 502 that has nothing to do with the video itself.
2. **Frequent page-format breakage.** PornHub changes its page bundle often enough that yt-dlp's own PornHub extractor needs a matching patch on a regular basis (yt-dlp's own issue tracker has multiple recurring "Unable to extract title" reports for exactly this). Keeping `yt-dlp` in `requirements.txt` pinned to a *recent* release — and bumping it when resolves start failing — is the only real mitigation.

Both failure modes surface identically here: a 502 from `/api/pornhub` with whatever message yt-dlp raised.

## API

### Root

```http
GET /
```

### Health

```http
GET /health
```

### Resolve

```http
GET /api/pornhub?url=<PORNHUB_VIDEO_URL>
```

Example:

```bash
curl --get 'http://localhost:8000/api/pornhub' \
  --data-urlencode 'url=https://www.pornhub.com/view_video.php?viewkey=000000000000000'
```

The response keeps yt-dlp's actual media URLs; the resolver does not invent a custom deep-link scheme.

## Run locally

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python app.py
```

Then open:

```text
http://127.0.0.1:8000/docs
```

## Docker

```bash
docker build -t pornhub-api .
docker run -d --name pornhub-api \
  --restart unless-stopped \
  -p 8000:8000 \
  -e PORT=8000 \
  pornhub-api
```

## Render / Railway

Use the Dockerfile deployment option. The application reads the platform's `PORT` environment variable.

For a VPS, put Nginx/Caddy in front of port `8000` and add HTTPS with your preferred certificate provider.

## Environment variables

```env
PORT=8000
RATE_LIMIT_PER_MINUTE=30
```

Do not commit secrets into Git.

## Notes

A source video can be private, removed, region-restricted, login-gated, rate-limited, or otherwise unavailable to yt-dlp. In those cases the API returns HTTP `502` with the resolver error instead of pretending that a link was resolved.
