import io
import unittest
import zipfile
from unittest.mock import AsyncMock, patch

import httpx

from app import _remember, albums, app, assets


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        assets.clear()
        albums.clear()

    async def test_home_and_health(self):
        home = await self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("轻取素材", home.text)
        self.assertIn("EARL XU", home.text)
        self.assertIn('property="og:image"', home.text)
        self.assertIn("https://douyin.earlxu.online/static/og-image.png?v=20261106-3", home.text)
        self.assertIn('href="/static/style.css?v=20261106-4"', home.text)
        self.assertIn('src="/static/app.js?v=20261106-2"', home.text)
        self.assertIn('href="/static/favicon.svg"', home.text)
        favicon = await self.client.get("/static/favicon.svg")
        self.assertEqual(favicon.status_code, 200)
        self.assertIn("image/svg+xml", favicon.headers["content-type"])
        social_image = await self.client.get("/static/og-image.png")
        self.assertEqual(social_image.status_code, 200)
        self.assertIn("image/png", social_image.headers["content-type"])
        health = await self.client.get("/health")
        self.assertEqual(health.json(), {"status": "ok"})

    async def test_parse_returns_tokenized_download(self):
        work = {
            "id": "7690898792699535546",
            "kind": "video",
            "title": "测试",
            "author": "月腕凝",
            "cover_url": None,
            "duration_seconds": 14,
            "media": [{
                "kind": "video",
                "url": "https://v11-default.365yg.com/video.mp4",
                "fallback_sources": ["https://v12-default.365yg.com/video.mp4"],
                "watermark_source": "https://v11-default.365yg.com/logo.mp4?logo_type=aweme",
            }],
        }
        with patch("app.parse_share", new=AsyncMock(return_value=work)):
            response = await self.client.post("/api/parse", json={"text": "https://v.douyin.com/test/"})
        self.assertEqual(response.status_code, 200)
        item = response.json()["media"][0]
        self.assertEqual(item["direct_url"], "https://v11-default.365yg.com/video.mp4")
        self.assertTrue(item["download_url"].startswith("/asset/"))
        self.assertEqual(item["filename"], "月腕凝_7690898792699535546.mp4")
        self.assertNotIn("url", item)
        self.assertNotIn("watermark_source", item)
        self.assertNotIn("fallback_sources", item)
        token = item["download_url"].split("/asset/")[1].split("?")[0]
        self.assertEqual(assets[token].filename, "月腕凝_7690898792699535546.mp4")
        self.assertEqual(len(assets[token].urls), 2)
        marked_token = item["watermarked"]["download_url"].split("/asset/")[1].split("?")[0]
        self.assertEqual(assets[marked_token].filename, "月腕凝_7690898792699535546_带水印.mp4")
        self.assertEqual(item["watermarked"]["filename"], "月腕凝_7690898792699535546_带水印.mp4")
        self.assertNotEqual(marked_token, token)

    async def test_preview_uses_next_cdn_mirror_after_failure(self):
        first = "https://v11-default.365yg.com/video.mp4"
        second = "https://v12-default.365yg.com/video.mp4"
        preview_url = _remember(first, "test.mp4", [second])["preview_url"]
        responses = [
            httpx.Response(502, text="upstream error"),
            httpx.Response(206, content=b"video", headers={
                "content-type": "video/mp4",
                "content-range": "bytes 0-4/100",
            }),
        ]
        with patch("app._open_media", new=AsyncMock(side_effect=responses)) as open_media:
            response = await self.client.get(preview_url, headers={"Range": "bytes=0-4"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"video")
        self.assertEqual([call.args[1] for call in open_media.await_args_list], [first, second])

    async def test_gallery_names_are_unique_and_safe(self):
        work = {
            "id": "7691709407915329381",
            "kind": "images",
            "title": "测试图集",
            "author": "作者/测试:作品",
            "cover_url": None,
            "duration_seconds": None,
            "media": [
                {"kind": "image", "url": "https://p11.douyinpic.com/one.jpg"},
                {"kind": "image", "url": "https://p11.douyinpic.com/two.jpg"},
            ],
        }
        with patch("app.parse_share", new=AsyncMock(return_value=work)):
            response = await self.client.post("/api/parse", json={"text": "https://v.douyin.com/test/"})
        self.assertEqual(response.status_code, 200)
        names = [assets[item["download_url"].split("/asset/")[1].split("?")[0]].filename for item in response.json()["media"]]
        self.assertEqual(names, [
            "作者_测试_作品_7691709407915329381_01.jpg",
            "作者_测试_作品_7691709407915329381_02.jpg",
        ])
        album_url = response.json()["album_download_url"]
        self.assertTrue(album_url.startswith("/album/"))
        self.assertEqual(len(albums[album_url.removeprefix("/album/")].images), 2)

        responses = [
            httpx.Response(502, text="first CDN failed"),
            httpx.Response(200, content=b"first image", headers={"content-type": "image/webp"}),
            httpx.Response(200, content=b"second image", headers={"content-type": "image/jpeg"}),
        ]
        first = albums[album_url.removeprefix("/album/")].images[0]
        first.urls = (first.urls[0], "https://p12.douyinpic.com/one.webp")
        with patch("app._open_media", new=AsyncMock(side_effect=responses)):
            archive_response = await self.client.get(album_url)
        self.assertEqual(archive_response.status_code, 200)
        self.assertEqual(archive_response.headers["content-type"], "application/zip")
        self.assertIn(".zip", archive_response.headers["content-disposition"])
        with zipfile.ZipFile(io.BytesIO(archive_response.content)) as bundle:
            self.assertEqual(bundle.namelist(), [
                "作者_测试_作品_7691709407915329381_01.webp",
                "作者_测试_作品_7691709407915329381_02.jpg",
            ])
            self.assertEqual(bundle.read(bundle.namelist()[0]), b"first image")
            self.assertEqual(bundle.read(bundle.namelist()[1]), b"second image")

    async def test_unknown_asset_is_rejected(self):
        response = await self.client.get("/asset/unknown?download=1")
        self.assertEqual(response.status_code, 404)
        album_response = await self.client.get("/album/unknown")
        self.assertEqual(album_response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
