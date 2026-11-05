# Deployment

The application is a single Python process and can run with systemd behind any HTTPS reverse proxy.

## Layout

```text
/opt/douyin-media-extractor/
├── current -> releases/<release>
├── releases/
├── venv/
├── data/
└── .env
```

## Systemd

1. Create a dedicated system user.
2. Create a project-local virtual environment and install `requirements.txt`.
3. Copy `production.env.example` to `.env` and adjust `DOUYIN_DATA_DIR`.
4. Install and adjust `douyin-media-extractor.service`.
5. Start the service and check `/health`.

The example service listens only on `127.0.0.1:8765`. Publish it through an HTTPS reverse proxy; do not expose the application port directly.

Install `ffprobe` if you want the server to verify higher-quality video streams that are not explicitly listed by the platform.

## Persistent Data

Keep `data/` across releases. It stores only the anonymous visitor identifier used by Douyin.

Never commit `.env`, `data/`, `.local/`, `.venv/`, caches, or logs.

## Rollback

Point `current` to the previous verified release, restart the service, and check `/health`.
