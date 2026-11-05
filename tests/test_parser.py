import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from parser import (
    ParseError, _guest_ttwid, _load_guest_cookie, _refresh_guest_cookie,
    _resolve_watermarked_source, _save_guest_cookie, _verified_1080_candidate,
    extract_share_url, normalize_work, parse_share, valid_media_url,
)


class ParserTests(unittest.IsolatedAsyncioTestCase):
    def test_share_text_and_url_validation(self):
        text = "复制打开抖音 https://v.douyin.com/zv8UwnMBkgM/ N@W.ZM"
        self.assertEqual(extract_share_url(text), "https://v.douyin.com/zv8UwnMBkgM")
        with self.assertRaises(ParseError):
            extract_share_url("https://example.com/not-douyin")
        self.assertTrue(valid_media_url("https://v11-default.365yg.com/video.mp4"))
        self.assertTrue(valid_media_url("https://v3-dy-o.zjcdn.com/video.mp4"))
        self.assertFalse(valid_media_url("https://evil.example/secret"))
        self.assertFalse(valid_media_url("https://365yg.com.evil.example/video.mp4"))
        self.assertFalse(valid_media_url("http://127.0.0.1/secret"))

    def test_video_prefers_play_address_without_logo_overlay(self):
        work = {
            "desc": "测试视频",
            "author": {"nickname": "作者"},
            "video": {
                "duration": 14000,
                "width": 1080,
                "height": 1920,
                "download_addr": {"url_list": ["https://v11-default.365yg.com/video/cn/mps/logo/main.mp4?logo_type=aweme"]},
                "play_addr": {"width": 720, "height": 1280, "url_list": [
                    "https://v11-default.365yg.com/play.mp4",
                    "https://v12-default.365yg.com/play.mp4",
                ]},
                "cover": {"url_list": ["https://p11.douyinpic.com/cover.jpeg"]},
            },
        }
        result = normalize_work(work, "7690898792699535546")
        self.assertEqual(result["kind"], "video")
        self.assertEqual(result["media"][0]["url"], "https://v11-default.365yg.com/play.mp4")
        self.assertEqual(result["media"][0]["fallback_sources"], ["https://v12-default.365yg.com/play.mp4"])
        self.assertEqual(result["media"][0]["reported_resolution"], "1080×1920")
        self.assertEqual(
            result["media"][0]["watermark_source"],
            "https://v11-default.365yg.com/video/cn/mps/logo/main.mp4?logo_type=aweme",
        )
        self.assertEqual(result["duration_seconds"], 14)

    def test_unmarked_download_address_does_not_claim_watermark(self):
        work = {
            "video": {
                "download_addr": {"url_list": ["https://v11-default.365yg.com/other.mp4"]},
                "play_addr": {"url_list": ["https://v11-default.365yg.com/play.mp4"]},
            },
        }
        media = normalize_work(work, "7690898792699535546")["media"][0]
        self.assertNotIn("watermark_source", media)

    def test_image_gallery(self):
        work = {
            "desc": "测试图集",
            "images": [
                {"url_list": ["https://p11.douyinpic.com/one.webp"]},
                {"origin_image": {"url_list": ["https://p11.douyinpic.com/two.webp"]}},
            ],
        }
        result = normalize_work(work, "7691709407915329381")
        self.assertEqual(result["kind"], "images")
        self.assertEqual(len(result["media"]), 2)

    def test_image_gallery_prefers_original_image(self):
        work = {
            "desc": "测试原图",
            "images": [{
                "url_list": ["https://p11.douyinpic.com/thumbnail.webp"],
                "display_image": {
                    "width": 720,
                    "height": 960,
                    "url_list": ["https://p11.douyinpic.com/display.webp"],
                },
                "origin_image": {
                    "width": 1440,
                    "height": 1920,
                    "url_list": [
                        "https://p11.douyinpic.com/original.webp",
                        "https://p12.douyinpic.com/original.webp",
                    ],
                },
            }],
        }
        media = normalize_work(work, "7691709407915329381")["media"][0]
        self.assertEqual(media["url"], "https://p11.douyinpic.com/original.webp")
        self.assertEqual(media["fallback_sources"], ["https://p12.douyinpic.com/original.webp"])
        self.assertEqual(media["quality"], "原图 1440×1920")

    def test_highest_resolution_download_with_h264_preview(self):
        work = {
            "video": {
                "duration": 10000,
                "play_addr_h264": {
                    "width": 720, "height": 1280, "data_size": 1500000,
                    "url_list": ["https://v11-default.365yg.com/h264.mp4"],
                },
                "bit_rate": [{
                    "is_h265": 1,
                    "bit_rate": 800000,
                    "play_addr": {
                        "width": 1080, "height": 1920, "data_size": 1000000,
                        "url_list": ["https://v11-default.365yg.com/h265.mp4"],
                    },
                }],
            },
        }
        media = normalize_work(work, "7690898792699535546")["media"][0]
        self.assertEqual(media["url"], "https://v11-default.365yg.com/h265.mp4")
        self.assertEqual(media["preview_source"], "https://v11-default.365yg.com/h264.mp4")
        self.assertEqual(media["quality"], "1080×1920 · H.265")

    def test_verified_1080_stream_replaces_lower_listed_variant(self):
        work = {"video": {
            "width": 1080, "height": 1920,
            "play_addr": {
                "width": 720, "height": 1280,
                "url_list": ["https://v11-default.365yg.com/720.mp4"],
            },
        }}
        verified = {
            "url": "https://v3-dy-o.zjcdn.com/1080.mp4",
            "mirrors": ["https://v3-dy-o.zjcdn.com/1080.mp4"],
            "width": 1080, "height": 1920, "codec": "H.264", "bitrate": 2000000, "size": 0,
        }
        media = normalize_work(work, "7690898792699535546", verified)["media"][0]
        self.assertEqual(media["quality"], "1080×1920 · H.264")
        self.assertEqual(media["url"], verified["url"])
        self.assertNotIn("reported_resolution", media)

    async def test_missing_ffprobe_keeps_listed_quality(self):
        work = {"video": {
            "width": 1080, "height": 1920,
            "play_addr": {
                "width": 720, "height": 1280,
                "uri": "v1e00fgi0000datouvnog65nhcji3l9g",
                "url_list": ["https://v11-default.365yg.com/720.mp4"],
            },
        }}
        with patch("parser.shutil.which", return_value=None):
            self.assertIsNone(await _verified_1080_candidate(work))

    async def test_feed_does_not_accept_recommended_video(self):
        with patch("parser._fetch_feed", new=AsyncMock(return_value=None)), patch(
            "parser._fetch_share_page", new=AsyncMock(return_value=None)
        ), patch(
            "parser._refresh_guest_cookie", new=AsyncMock(return_value="guest-cookie")
        ):
            with self.assertRaises(ParseError):
                await parse_share("https://www.douyin.com/video/7690898792699535546")

    def test_guest_cookie_is_persisted_privately_and_expires(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "parser.GUEST_COOKIE_FILE", Path(directory) / ".local" / "ttwid.json"
        ) as cookie_file:
            _save_guest_cookie("visitor-token-123", time.time() + 3600)
            self.assertEqual(_load_guest_cookie(), "visitor-token-123")
            self.assertEqual(os.stat(cookie_file).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(cookie_file.parent).st_mode & 0o777, 0o700)
            _save_guest_cookie("visitor-token-123", time.time() - 1)
            self.assertIsNone(_load_guest_cookie())

    def test_guest_ttwid_accepts_bytedance_register_cookie(self):
        cookies = httpx.Cookies()
        cookies.set("ttwid", "guest-token", domain=".bytedance.com")
        cookie = _guest_ttwid(cookies)
        self.assertIsNotNone(cookie)
        self.assertEqual(cookie.value, "guest-token")

    async def test_guest_cookie_refresh_uses_ttwid_register_endpoint(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

        class FakeClient:
            def __init__(self):
                self.cookies = httpx.Cookies()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, *args, **kwargs):
                self.cookies.set("ttwid", "registered-guest", domain=".bytedance.com")
                return FakeResponse()

        with tempfile.TemporaryDirectory() as directory, patch(
            "parser.GUEST_COOKIE_FILE", Path(directory) / "ttwid.json"
        ), patch("parser.httpx.AsyncClient", return_value=FakeClient()):
            value = await _refresh_guest_cookie("video", "7690898792699535546", None)
            self.assertEqual(value, "registered-guest")
            self.assertEqual(_load_guest_cookie(), "registered-guest")

    async def test_media_redirect_prefers_cors_capable_https_cdn(self):
        final = "https://v11-default.365yg.com/watermarked.mp4"

        def handler(request):
            if "/play/" in request.url.path or "/playwm/" in request.url.path:
                return httpx.Response(302, headers={"location": final})
            return httpx.Response(
                206,
                headers={
                    "content-type": "video/mp4",
                    "access-control-allow-origin": "*",
                    "content-range": "bytes 0-0/100",
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            for path in ("play", "playwm"):
                with self.subTest(path=path):
                    result = await _resolve_watermarked_source(
                        client,
                        f"https://aweme.snssdk.com/aweme/v1/{path}/?video_id=test",
                    )
                    self.assertEqual(result, final)

    async def test_guest_cookie_is_reused_then_refreshed_if_empty(self):
        work = {"aweme_id": "7690898792699535546", "video": {
            "play_addr": {"url_list": ["https://v11-default.365yg.com/play.mp4"]},
        }}
        with tempfile.TemporaryDirectory() as directory, patch(
            "parser.GUEST_COOKIE_FILE", Path(directory) / "ttwid.json"
        ):
            _save_guest_cookie("old-visitor-token", time.time() + 3600)
            with patch("parser._fetch_feed", new=AsyncMock(return_value=None)), patch(
                "parser._fetch_share_page", new=AsyncMock(side_effect=[None, work])
            ) as share, patch(
                "parser._refresh_guest_cookie", new=AsyncMock(return_value="new-visitor-token")
            ) as refresh:
                result = await parse_share("https://www.douyin.com/video/7690898792699535546")
            self.assertEqual(result["kind"], "video")
            self.assertEqual(share.await_args_list[0].args[3], "old-visitor-token")
            self.assertEqual(share.await_args_list[1].args[3], "new-visitor-token")
            self.assertEqual(refresh.await_args.args[2], "old-visitor-token")


if __name__ == "__main__":
    unittest.main()
