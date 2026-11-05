"""Small local web UI for resolving and downloading public Douyin media."""

from __future__ import annotations

import logging
import re
import secrets
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urljoin

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from parser import APP_UA, ParseError, parse_share, valid_media_url


BASE_DIR = Path(__file__).parent
TOKEN_TTL_SECONDS = 20 * 60
MAX_MEDIA_BYTES = 300 * 1024 * 1024
MAX_IMAGE_BYTES = 30 * 1024 * 1024
logger = logging.getLogger(__name__)


@dataclass
class Asset:
    urls: tuple[str, ...]
    filename: str
    expires_at: float


@dataclass
class Album:
    images: tuple[Asset, ...]
    filename: str
    expires_at: float


assets: dict[str, Asset] = {}
albums: dict[str, Album] = {}
app = FastAPI(title="Douyin Media Extractor", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class ParseRequest(BaseModel):
    text: str = Field(min_length=1, max_length=3000)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; media-src 'self'; "
        "script-src 'self'; style-src 'self'; connect-src 'self' "
        "https://*.365yg.com https://*.amemv.com https://*.douyinvod.com "
        "https://*.idouyinvod.com https://*.douyinpic.com https://*.byteimg.com "
        "https://*.bytedance.net https://*.bytedtos.com https://*.snssdk.com "
        "https://*.volccdn.com https://*.pstatp.com https://*.bytecdn.cn "
        "https://*.bytecdn.com https://*.zjcdn.com; base-uri 'none'"
    )
    return response


@app.get("/")
async def home():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


def _remember(url: str, filename: str, fallback_urls: list[str] | None = None) -> dict:
    urls = tuple(dict.fromkeys([url, *(fallback_urls or [])]))
    if not all(valid_media_url(item) for item in urls):
        raise ParseError("平台返回了不受支持的媒体地址。")
    now = time.monotonic()
    for key, asset in list(assets.items()):
        if asset.expires_at <= now:
            del assets[key]
    token = secrets.token_urlsafe(24)
    assets[token] = Asset(urls=urls, filename=filename, expires_at=now + TOKEN_TTL_SECONDS)
    return {
        "direct_url": url,
        "preview_url": f"/asset/{token}",
        "download_url": f"/asset/{token}?download=1",
        "filename": filename,
    }


def _safe_filename_part(value: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("._-")
    return cleaned[:60] or "douyin"


def _remember_album(images: list[Asset], filename: str) -> str:
    now = time.monotonic()
    for key, album in list(albums.items()):
        if album.expires_at <= now:
            del albums[key]
    token = secrets.token_urlsafe(24)
    albums[token] = Album(images=tuple(images), filename=filename, expires_at=now + TOKEN_TTL_SECONDS)
    return f"/album/{token}"


def _filename_for_type(filename: str, content_type: str) -> str:
    extension = {
        "image/webp": "webp",
        "image/png": "png",
        "image/gif": "gif",
        "image/jpeg": "jpg",
    }.get(content_type)
    return filename.rsplit(".", 1)[0] + "." + extension if extension else filename


def _attachment_header(filename: str) -> str:
    ascii_filename = filename.encode("ascii", "ignore").decode("ascii") or "douyin_media"
    return f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{quote(filename)}'


@app.post("/api/parse")
async def parse(request: ParseRequest):
    try:
        result = await parse_share(request.text)
        filename_base = f"{_safe_filename_part(result['author'])}_{result['id']}"
        album_images = []
        for index, media in enumerate(result["media"], 1):
            ext = "mp4" if media["kind"] == "video" else "jpg"
            suffix = f"_{index:02d}" if result["kind"] == "images" else ""
            preview_source = media.pop("preview_source", None)
            preview_fallbacks = media.pop("preview_fallback_sources", [])
            fallback_sources = media.pop("fallback_sources", [])
            watermark_source = media.pop("watermark_source", None)
            watermark_fallbacks = media.pop("watermark_fallback_sources", [])
            remembered = _remember(media["url"], f"{filename_base}{suffix}.{ext}", fallback_sources)
            media.update(remembered)
            if result["kind"] == "images" and media["kind"] == "image":
                album_images.append(assets[remembered["preview_url"].removeprefix("/asset/")])
            if preview_source and preview_source != media["url"]:
                media["preview_url"] = _remember(preview_source, f"{filename_base}{suffix}_preview.mp4", preview_fallbacks)["preview_url"]
            if watermark_source:
                media["watermarked"] = _remember(watermark_source, f"{filename_base}{suffix}_带水印.mp4", watermark_fallbacks)
            del media["url"]
        if result["cover_url"]:
            result["cover"] = _remember(result["cover_url"], f"{filename_base}_cover.jpg")
        else:
            result["cover"] = None
        del result["cover_url"]
        result["album_download_url"] = (
            _remember_album(album_images, f"{filename_base}_图集.zip") if album_images else None
        )
        result["source_url"] = f"https://www.douyin.com/{'note' if result['kind'] == 'images' else 'video'}/{result['id']}"
        return result
    except ParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Douyin request failed: %s", exc)
        raise HTTPException(status_code=502, detail="连接抖音失败，请稍后重试。") from exc


async def _open_media(client: httpx.AsyncClient, url: str, headers: dict) -> httpx.Response:
    for _ in range(4):
        if not valid_media_url(url):
            raise HTTPException(status_code=502, detail="媒体地址不在允许的域名内。")
        request = client.build_request("GET", url, headers=headers)
        response = await client.send(request, stream=True)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "")
            await response.aclose()
            if not location:
                break
            url = urljoin(url, location)
            continue
        return response
    raise HTTPException(status_code=502, detail="媒体地址跳转异常。")


@app.get("/asset/{token}")
async def get_asset(request: Request, token: str, download: bool = Query(False)):
    asset = assets.get(token)
    if asset is None or asset.expires_at <= time.monotonic():
        raise HTTPException(status_code=404, detail="链接已过期，请重新解析作品。")

    upstream_headers = {
        "User-Agent": APP_UA,
        "Referer": "https://www.douyin.com/",
    }
    range_header = request.headers.get("range", "")
    if not download and re.fullmatch(r"bytes=\d+-\d*", range_header):
        upstream_headers["Range"] = range_header

    client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=False)
    try:
        for url in asset.urls:
            try:
                candidate = await _open_media(client, url, upstream_headers)
            except (httpx.HTTPError, HTTPException):
                continue
            content_type = candidate.headers.get("content-type", "").split(";")[0].lower()
            if candidate.status_code in (200, 206) and (
                content_type.startswith("video/")
                or content_type.startswith("image/")
                or content_type == "application/octet-stream"
            ):
                upstream = candidate
                break
            await candidate.aclose()
        else:
            raise HTTPException(status_code=502, detail="媒体源暂时不可用，请重新解析后再试。")
        length = upstream.headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_MEDIA_BYTES:
            await upstream.aclose()
            raise HTTPException(status_code=413, detail="文件超过本服务的 300 MB 限制。")
    except Exception:
        await client.aclose()
        raise

    async def body():
        total = 0
        try:
            async for chunk in upstream.aiter_bytes():
                total += len(chunk)
                if total > MAX_MEDIA_BYTES:
                    break
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    headers = {"Cache-Control": "private, no-store", "Accept-Ranges": "bytes"}
    for name in ("content-length", "content-range"):
        if value := upstream.headers.get(name):
            headers[name] = value
    if download:
        headers["Content-Disposition"] = _attachment_header(_filename_for_type(asset.filename, content_type))
    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        headers=headers,
        media_type=content_type,
    )


async def _read_album_image(client: httpx.AsyncClient, asset: Asset, remaining: int) -> tuple[bytes, str]:
    headers = {"User-Agent": APP_UA, "Referer": "https://www.douyin.com/"}
    for url in asset.urls:
        try:
            upstream = await _open_media(client, url, headers)
        except (httpx.HTTPError, HTTPException):
            continue
        try:
            content_type = upstream.headers.get("content-type", "").split(";")[0].lower()
            if upstream.status_code != 200 or not content_type.startswith("image/"):
                continue
            limit = min(MAX_IMAGE_BYTES, remaining)
            length = upstream.headers.get("content-length", "")
            if length.isdigit() and int(length) > limit:
                raise HTTPException(status_code=413, detail="图集超过本服务的大小限制。")
            data = bytearray()
            async for chunk in upstream.aiter_bytes():
                if len(data) + len(chunk) > limit:
                    raise HTTPException(status_code=413, detail="图集超过本服务的大小限制。")
                data.extend(chunk)
            return bytes(data), content_type
        except httpx.HTTPError:
            continue
        finally:
            await upstream.aclose()
    raise HTTPException(status_code=502, detail="图集中有图片暂时不可用，请重新解析后再试。")


@app.get("/album/{token}")
async def get_album(token: str):
    album = albums.get(token)
    if album is None or album.expires_at <= time.monotonic():
        raise HTTPException(status_code=404, detail="图集链接已过期，请重新解析作品。")
    archive = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b")
    try:
        total = 0
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=False) as client:
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
                for asset in album.images:
                    data, content_type = await _read_album_image(client, asset, MAX_MEDIA_BYTES - total)
                    total += len(data)
                    bundle.writestr(_filename_for_type(asset.filename, content_type), data)
        archive.seek(0, 2)
        size = archive.tell()
        archive.seek(0)
    except Exception:
        archive.close()
        raise

    async def body():
        try:
            while chunk := archive.read(64 * 1024):
                yield chunk
        finally:
            archive.close()

    return StreamingResponse(
        body(),
        media_type="application/zip",
        headers={
            "Content-Length": str(size),
            "Cache-Control": "private, no-store",
            "Content-Disposition": _attachment_header(album.filename),
        },
    )
