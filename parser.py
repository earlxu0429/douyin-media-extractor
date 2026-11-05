"""Resolve a public Douyin share link to downloadable media without account cookies."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

import httpx


SHARE_HOSTS = {
    "v.douyin.com",
    "www.douyin.com",
    "m.douyin.com",
    "www.iesdouyin.com",
    "iesdouyin.com",
    "jingxuan.douyin.com",
}
MEDIA_HOST_SUFFIXES = (
    "365yg.com",
    "amemv.com",
    "douyinvod.com",
    "idouyinvod.com",
    "douyinpic.com",
    "byteimg.com",
    "bytedance.net",
    "bytedtos.com",
    "snssdk.com",
    "volccdn.com",
    "pstatp.com",
    "bytecdn.cn",
    "bytecdn.com",
    "zjcdn.com",
)
URL_RE = re.compile(r"https?://[^\s<>\"'\]\)]+", re.I)
WORK_RE = re.compile(r"/(video|note|slides)/(\d{15,})")
TTWID_REGISTER_URL = "https://ttwid.bytedance.com/ttwid/union/register/"
TTWID_COOKIE_DOMAINS = ("bytedance.com", "douyin.com", "iesdouyin.com")
APP_UA = (
    "com.ss.android.ugc.aweme/410000 (Linux; U; Android 13; zh_CN; "
    "Pixel 7; Build/TQ3A.230805.001; Cronet/TTNetVersion:1.0.0.0)"
)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 Version/17.4 Mobile/15E148 Safari/604.1"
)
DATA_DIR = Path(os.environ.get("DOUYIN_DATA_DIR", str(Path(__file__).parent / ".local"))).expanduser()
GUEST_COOKIE_FILE = DATA_DIR / "ttwid.json"
_guest_cookie_lock = asyncio.Lock()


class ParseError(Exception):
    pass


def _load_guest_cookie() -> str | None:
    try:
        saved = json.loads(GUEST_COOKIE_FILE.read_text(encoding="utf-8"))
        value = saved.get("ttwid")
        if saved.get("expires_at", 0) > time.time() and isinstance(value, str):
            if 10 <= len(value) <= 1000 and not re.search(r"[\s;]", value):
                return value
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def _save_guest_cookie(value: str, expires_at: float) -> None:
    directory = GUEST_COOKIE_FILE.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".ttwid-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"ttwid": value, "expires_at": expires_at}, handle)
        os.replace(temporary, GUEST_COOKIE_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _share_url(kind: str, work_id: str) -> str:
    page_kind = "note" if kind in ("note", "slides") else "video"
    return f"https://www.iesdouyin.com/share/{page_kind}/{work_id}/"


def _guest_ttwid(cookies: httpx.Cookies):
    for cookie in cookies.jar:
        domain = cookie.domain.lstrip(".").lower()
        if cookie.name == "ttwid" and _host_matches(domain, TTWID_COOKIE_DOMAINS):
            return cookie
    return None


async def _refresh_guest_cookie(kind: str, work_id: str, stale: str | None) -> str | None:
    async with _guest_cookie_lock:
        current = _load_guest_cookie()
        if current and current != stale:
            return current
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            try:
                response = await client.post(
                    TTWID_REGISTER_URL,
                    json={
                        "region": "cn",
                        "aid": 1768,
                        "needFid": False,
                        "service": "www.douyin.com",
                        "migrate_info": {"ticket": "", "source": "node"},
                        "cbUrlProtocol": "https",
                        "union": True,
                    },
                    headers={"User-Agent": MOBILE_UA},
                )
                response.raise_for_status()
            except (httpx.HTTPError, ValueError):
                pass
            cookie = _guest_ttwid(client.cookies)
            if cookie is None:
                response = await client.get(
                    _share_url(kind, work_id),
                    headers={"User-Agent": MOBILE_UA},
                )
                response.raise_for_status()
                cookie = _guest_ttwid(client.cookies)
            if cookie is not None:
                expires_at = cookie.expires or time.time() + 365 * 86400
                _save_guest_cookie(cookie.value, expires_at)
                return cookie.value
    return None


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def valid_media_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
            and _host_matches((parsed.hostname or "").lower(), MEDIA_HOST_SUFFIXES)
        )
    except ValueError:
        return False


def extract_share_url(text: str) -> str:
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:!?/。，；：！？")
        parsed = urlsplit(url)
        if parsed.scheme == "https" and parsed.hostname in SHARE_HOSTS and not parsed.username:
            return url
    raise ParseError("请粘贴抖音短链、作品链接或完整分享文案。")


def _work_from_url(url: str) -> tuple[str, str] | None:
    parsed = urlsplit(url)
    match = WORK_RE.search(parsed.path)
    if match:
        return match.group(1), match.group(2)
    query_match = re.search(r"(?:^|&)modal_id=(\d{15,})(?:&|$)", parsed.query)
    if query_match:
        return "video", query_match.group(1)
    return None


async def resolve_work(client: httpx.AsyncClient, share_text: str) -> tuple[str, str]:
    url = extract_share_url(share_text)
    for _ in range(4):
        work = _work_from_url(url)
        if work:
            return work
        parsed = urlsplit(url)
        if parsed.hostname not in SHARE_HOSTS:
            raise ParseError("短链跳转到了非抖音网站，已停止解析。")
        response = await client.get(url, headers={"User-Agent": MOBILE_UA}, follow_redirects=False)
        if response.status_code not in (301, 302, 303, 307, 308):
            break
        url = urljoin(url, response.headers.get("location", ""))
        if urlsplit(url).hostname not in SHARE_HOSTS:
            raise ParseError("短链跳转到了非抖音网站，已停止解析。")
    raise ParseError("无法从链接中找到作品 ID，可能不是视频或图集链接。")


def _first_url(value: object) -> str | None:
    if isinstance(value, str):
        return value if valid_media_url(value) else None
    if isinstance(value, dict):
        for key in ("url_list", "urlList"):
            urls = value.get(key)
            if isinstance(urls, list):
                for url in urls:
                    if isinstance(url, str) and valid_media_url(url):
                        return url
        for key in ("origin_image", "display_image", "download_url", "url"):
            url = _first_url(value.get(key))
            if url:
                return url
    return None


def _url_mirrors(value: object) -> list[str]:
    if isinstance(value, dict):
        urls = value.get("url_list") or value.get("urlList") or []
        mirrors = list(dict.fromkeys(url for url in urls if isinstance(url, str) and valid_media_url(url)))
        if mirrors:
            return mirrors
    first = _first_url(value)
    return [first] if first else []


def _positive_int(value: object) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _best_image_source(image: dict) -> dict | None:
    sources = [
        (image.get("origin_image"), "原图", 3),
        (image.get("download_url"), "下载原图", 2),
        (image.get("display_image"), "显示图", 1),
        (image, "图片", 0),
    ]
    candidates = []
    seen = set()
    for source, label, priority in sources:
        mirrors = _url_mirrors(source)
        if not mirrors or mirrors[0] in seen:
            continue
        seen.add(mirrors[0])
        width = _positive_int(source.get("width")) if isinstance(source, dict) else 0
        height = _positive_int(source.get("height")) if isinstance(source, dict) else 0
        size = (
            _positive_int(source.get("data_size") or source.get("file_size"))
            if isinstance(source, dict)
            else 0
        )
        candidates.append({
            "url": mirrors[0],
            "fallback_sources": mirrors[1:],
            "width": width,
            "height": height,
            "size": size,
            "label": label,
            "priority": priority,
        })
    if not candidates:
        return None
    best = max(candidates, key=lambda item: (item["priority"], item["width"] * item["height"], item["size"]))
    resolution = f"{best['width']}×{best['height']}" if best["width"] and best["height"] else ""
    return {
        "kind": "image",
        "url": best["url"],
        "fallback_sources": best["fallback_sources"],
        "quality": f"{best['label']} {resolution}".strip(),
    }


def _image_entries(work: dict) -> list[dict]:
    info = work.get("image_post_info") or {}
    images = (
        info.get("images")
        or info.get("image_list")
        or work.get("images")
        or work.get("image_list")
        or []
    )
    entries = []
    for image in images[:30]:
        if isinstance(image, dict):
            best_image = _best_image_source(image)
            if best_image:
                entries.append(best_image)
        else:
            image_url = _first_url(image)
            if image_url:
                entries.append({"kind": "image", "url": image_url})
        if isinstance(image, dict):
            video = image.get("video") or {}
            video_candidates = _video_candidates(video)
            best_live = max(
                video_candidates,
                key=lambda item: (item["width"] * item["height"], item["bitrate"], item["size"]),
            ) if video_candidates else None
            if best_live:
                entries.append({
                    "kind": "video",
                    "url": best_live["url"],
                    "fallback_sources": best_live["mirrors"][1:],
                    "quality": (
                        f"{best_live['width']}×{best_live['height']} · {best_live['codec']}"
                        if best_live["width"] and best_live["height"]
                        else best_live["codec"]
                    ),
                })
    return entries


def _video_candidates(video: dict) -> list[dict]:
    duration_ms = video.get("duration") or 0
    candidates = []
    sources = [
        (video.get("play_addr_h264"), "H.264", None),
        (video.get("play_addr_265"), "H.265", None),
        (video.get("play_addr"), "H.265" if video.get("is_h265") else "H.264", None),
    ]
    for variant in video.get("bit_rate") or []:
        sources.append(
            (variant.get("play_addr"), "H.265" if variant.get("is_h265") else "H.264", variant.get("bit_rate"))
        )
    seen = set()
    for address, codec, bitrate in sources:
        if not isinstance(address, dict):
            continue
        mirrors = [url.replace("playwm", "play") for url in _url_mirrors(address)]
        mirrors = list(dict.fromkeys(url for url in mirrors if "/mps/logo/" not in url and "logo_type=" not in url))
        if not mirrors:
            continue
        url = mirrors[0]
        if url in seen:
            continue
        seen.add(url)
        size = address.get("data_size") or 0
        estimated_bitrate = round(size * 8000 / duration_ms) if size and duration_ms else 0
        candidates.append(
            {
                "url": url,
                "codec": codec,
                "width": address.get("width") or 0,
                "height": address.get("height") or 0,
                "bitrate": bitrate or estimated_bitrate,
                "size": size,
                "mirrors": mirrors,
            }
        )
    return candidates


def _watermarked_urls(video: dict) -> list[str]:
    for key in ("download_addr", "play_addr", "play_addr_h264", "play_addr_265"):
        mirrors = [
            url for url in _url_mirrors(video.get(key))
            if "/mps/logo/" in url or "logo_type=" in url or "playwm" in url
        ]
        if mirrors:
            return mirrors
    return []


async def _resolve_watermarked_source(client: httpx.AsyncClient, url: str) -> str | None:
    path = urlsplit(url).path
    if valid_media_url(url) and "/play/" not in path and "/playwm/" not in path:
        return url
    for _ in range(4):
        try:
            response = await client.get(url, headers={"User-Agent": APP_UA})
            location = response.headers.get("location", "")
            if not location:
                continue
            candidate = urljoin(url, location)
            if candidate.startswith("http://"):
                candidate = "https://" + candidate.removeprefix("http://")
            if not valid_media_url(candidate):
                continue
            async with client.stream(
                "GET",
                candidate,
                headers={
                    "User-Agent": APP_UA,
                    "Range": "bytes=0-0",
                    "Origin": "https://douyin.earlxu.online",
                },
            ) as media:
                if (
                    media.status_code in (200, 206)
                    and media.headers.get("content-type", "").startswith("video/")
                    and media.headers.get("access-control-allow-origin") == "*"
                ):
                    return candidate
        except (httpx.HTTPError, ValueError):
            continue
    return None


def normalize_work(work: dict, work_id: str, extra_candidate: dict | None = None) -> dict:
    images = _image_entries(work)
    video = work.get("video") or {}
    candidates = _video_candidates(video) if not images else []
    if extra_candidate and not images:
        candidates.append(extra_candidate)
    best = max(candidates, key=lambda item: (item["width"] * item["height"], item["bitrate"], item["size"])) if candidates else None
    compatible = [item for item in candidates if item["codec"] == "H.264"]
    preview = max(compatible, key=lambda item: (item["width"] * item["height"], item["bitrate"])) if compatible else best
    media = images or ([{
        "kind": "video",
        "url": best["url"],
        "fallback_sources": best["mirrors"][1:],
        "preview_source": preview["url"],
        "preview_fallback_sources": preview["mirrors"][1:],
        "quality": f"{best['width']}×{best['height']} · {best['codec']}" if best["width"] and best["height"] else best["codec"],
    }] if best else [])
    if best and not images:
        if (video.get("width"), video.get("height")) != (best["width"], best["height"]):
            if video.get("width") and video.get("height"):
                media[0]["reported_resolution"] = f"{video['width']}×{video['height']}"
        watermarked = _watermarked_urls(video)
        if watermarked and watermarked[0] != best["url"]:
            media[0]["watermark_source"] = watermarked[0]
            media[0]["watermark_fallback_sources"] = watermarked[1:]
    if not media:
        raise ParseError("已找到作品，但平台没有返回可下载的视频或图片。")
    cover = _first_url(video.get("cover"))
    if not cover and images:
        cover = images[0]["url"]
    author = work.get("author") or {}
    return {
        "id": work_id,
        "kind": "images" if images else "video",
        "title": work.get("desc") or work.get("title") or "未命名作品",
        "author": author.get("nickname") or "抖音创作者",
        "cover_url": cover,
        "duration_seconds": round(video.get("duration", 0) / 1000) if video.get("duration") else None,
        "media": media,
    }


async def _fetch_feed(client: httpx.AsyncClient, work_id: str) -> dict | None:
    response = await client.get(
        "https://aweme.snssdk.com/aweme/v1/feed/",
        params={"aweme_id": work_id},
        headers={"User-Agent": APP_UA},
    )
    response.raise_for_status()
    payload = response.json()
    for work in payload.get("aweme_list") or []:
        if str(work.get("aweme_id")) == work_id:
            return work
    return None


async def _fetch_share_page(client: httpx.AsyncClient, kind: str, work_id: str, ttwid: str) -> dict | None:
    client.cookies.clear()
    response = await client.get(
        _share_url(kind, work_id),
        headers={"User-Agent": MOBILE_UA, "Cookie": f"ttwid={ttwid}"},
    )
    response.raise_for_status()
    match = re.search(r"window\._ROUTER_DATA\s*=\s*", response.text)
    if not match:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(response.text[match.end():])
    except json.JSONDecodeError:
        return None
    for page in (payload.get("loaderData") or {}).values():
        if not isinstance(page, dict):
            continue
        for work in (page.get("videoInfoRes") or {}).get("item_list") or []:
            if str(work.get("aweme_id")) == work_id:
                return work
    return None


async def _probe_video(url: str, executable: str) -> dict | None:
    process = await asyncio.create_subprocess_exec(
        executable, "-v", "error", "-rw_timeout", "10000000", "-user_agent", APP_UA,
        "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height,bit_rate:format=bit_rate",
        "-of", "json", url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=12)
    except TimeoutError:
        process.kill()
        await process.communicate()
        return None
    if process.returncode != 0:
        return None
    try:
        payload = json.loads(output)
        stream = (payload.get("streams") or [])[0]
        return {
            "width": int(stream.get("width") or 0),
            "height": int(stream.get("height") or 0),
            "codec": {"h264": "H.264", "hevc": "H.265"}.get(stream.get("codec_name")),
            "bitrate": int((payload.get("format") or {}).get("bit_rate") or stream.get("bit_rate") or 0),
        }
    except (ValueError, IndexError, TypeError, AttributeError):
        return None


async def _verified_1080_candidate(work: dict) -> dict | None:
    video = work.get("video") or {}
    if min(video.get("width") or 0, video.get("height") or 0) < 1080:
        return None
    existing = _video_candidates(video)
    if any(item["width"] * item["height"] >= 1080 * 1920 for item in existing):
        return None
    uri = (video.get("play_addr") or {}).get("uri")
    executable = shutil.which("ffprobe")
    if not executable or not isinstance(uri, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,100}", uri):
        return None
    endpoint = "https://aweme.snssdk.com/aweme/v1/play/?" + urlencode(
        {"video_id": uri, "ratio": "1080p", "line": 0}
    )
    for _ in range(2):
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
                async with client.stream("GET", endpoint, headers={"User-Agent": APP_UA}) as response:
                    if response.status_code not in (301, 302, 303, 307, 308):
                        continue
                    location = urljoin(endpoint, response.headers.get("location", ""))
            if not valid_media_url(location) or "/mps/logo/" in location or "logo_type=" in location:
                continue
            details = await _probe_video(location, executable)
            if details and details["codec"] and details["width"] * details["height"] > max(
                (item["width"] * item["height"] for item in existing), default=0
            ):
                return {**details, "url": location, "mirrors": [location, endpoint], "size": 0}
        except (httpx.HTTPError, OSError):
            continue
    return None


async def parse_share(share_text: str) -> dict:
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        kind, work_id = await resolve_work(client, share_text)
        work = None
        try:
            work = await _fetch_feed(client, work_id)
        except (httpx.HTTPError, ValueError):
            pass
        if work is None:
            cached_cookie = _load_guest_cookie()
            if cached_cookie:
                try:
                    work = await _fetch_share_page(client, kind, work_id, cached_cookie)
                except (httpx.HTTPError, ValueError):
                    pass
            if work is None:
                try:
                    fresh_cookie = await _refresh_guest_cookie(kind, work_id, cached_cookie)
                    if fresh_cookie:
                        work = await _fetch_share_page(client, kind, work_id, fresh_cookie)
                except (httpx.HTTPError, ValueError):
                    pass
        if work is None:
            raise ParseError("平台未向游客返回这条作品的媒体数据；可能受到作品权限、地区或访问限制。")
        extra_candidate = await _verified_1080_candidate(work) if kind == "video" else None
        result = normalize_work(work, work_id, extra_candidate)
        for media in result["media"]:
            direct_source = media.get("url")
            if direct_source:
                resolved_direct = await _resolve_watermarked_source(client, direct_source)
                media["direct_download_available"] = bool(resolved_direct)
                if resolved_direct and resolved_direct != direct_source:
                    old_fallbacks = media.get("fallback_sources", [])
                    media["url"] = resolved_direct
                    media["fallback_sources"] = list(
                        dict.fromkeys([direct_source, *old_fallbacks])
                    )
            watermark_source = media.get("watermark_source")
            if not watermark_source:
                continue
            resolved = await _resolve_watermarked_source(client, watermark_source)
            media["watermark_direct_available"] = bool(resolved)
            if resolved:
                old_fallbacks = media.get("watermark_fallback_sources", [])
                media["watermark_source"] = resolved
                media["watermark_fallback_sources"] = list(
                    dict.fromkeys([watermark_source, *old_fallbacks])
                )
        return result
