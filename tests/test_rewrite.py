"""URL rewrite tests — no Discord token required."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import (
    apply_rewrites,
    is_instagram_embeddable,
    is_skipped_host,
    needs_resolve,
    parse_allowed_guilds,
    pick_source,
    rewrite_one,
    rewrite_resolved,
    strip_tracking,
)


class FakeResponse:
    def __init__(self, url: str) -> None:
        self.url = url

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeSession:
    def __init__(self, final_url: str) -> None:
        self.final_url = final_url
        self.heads = 0
        self.gets = 0

    def head(self, url: str, **kwargs: object) -> FakeResponse:
        self.heads += 1
        return FakeResponse(self.final_url)

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.gets += 1
        return FakeResponse(self.final_url)


class RewriteResolvedTests(unittest.TestCase):
    def test_twitter_and_x(self) -> None:
        self.assertEqual(
            rewrite_resolved("https://twitter.com/user/status/123"),
            "https://fixupx.com/user/status/123",
        )
        self.assertEqual(
            rewrite_resolved("https://x.com/user/status/123"),
            "https://fixupx.com/user/status/123",
        )
        self.assertEqual(
            rewrite_resolved("https://www.twitter.com/user/status/123"),
            "https://fixupx.com/user/status/123",
        )

    def test_instagram_paths(self) -> None:
        self.assertEqual(
            rewrite_resolved("https://www.instagram.com/p/AbC123/"),
            "https://oginstagram.com/p/AbC123/",
        )
        self.assertEqual(
            rewrite_resolved("https://instagram.com/reel/AbC123/"),
            "https://oginstagram.com/reel/AbC123/",
        )
        self.assertEqual(
            rewrite_resolved("https://instagram.com/reels/AbC123/"),
            "https://oginstagram.com/reels/AbC123/",
        )
        self.assertEqual(
            rewrite_resolved("https://instagram.com/tv/AbC123/"),
            "https://oginstagram.com/tv/AbC123/",
        )
        self.assertEqual(
            rewrite_resolved("https://instagram.com/someone/p/AbC123/"),
            "https://oginstagram.com/someone/p/AbC123/",
        )
        self.assertEqual(
            rewrite_resolved("https://instagram.com/someone/reel/AbC123/"),
            "https://oginstagram.com/someone/reel/AbC123/",
        )
        self.assertEqual(
            rewrite_resolved("https://instagram.com/stories/someone/123456/"),
            "https://oginstagram.com/stories/someone/123456/",
        )

    def test_instagram_skips_profiles_and_unresolved_share(self) -> None:
        self.assertIsNone(rewrite_resolved("https://instagram.com/someone/"))
        self.assertIsNone(rewrite_resolved("https://instagram.com/share/reel/AbC"))
        self.assertFalse(is_instagram_embeddable("/share/reel/AbC"))
        self.assertFalse(is_instagram_embeddable("/someone/"))

    def test_reddit_tiktok_bsky(self) -> None:
        self.assertEqual(
            rewrite_resolved("https://www.reddit.com/r/foo/comments/abc/title/"),
            "https://rxddit.com/r/foo/comments/abc/title/",
        )
        self.assertEqual(
            rewrite_resolved("https://old.reddit.com/r/foo/comments/abc/title/"),
            "https://rxddit.com/r/foo/comments/abc/title/",
        )
        self.assertEqual(
            rewrite_resolved("https://www.tiktok.com/@user/video/123"),
            "https://vxtiktok.com/@user/video/123",
        )
        self.assertEqual(
            rewrite_resolved("https://bsky.app/profile/user.bsky.social/post/abc"),
            "https://bskx.app/profile/user.bsky.social/post/abc",
        )

    def test_skip_already_mirrored(self) -> None:
        self.assertTrue(is_skipped_host("fixupx.com"))
        self.assertTrue(is_skipped_host("www.fixupx.com"))
        self.assertTrue(is_skipped_host("oginstagram.com"))
        self.assertTrue(is_skipped_host("g.oginstagram.com"))
        self.assertTrue(is_skipped_host("d.oginstagram.com"))
        self.assertIsNone(rewrite_resolved("https://fixupx.com/user/status/123"))
        self.assertIsNone(rewrite_resolved("https://oginstagram.com/p/AbC123/"))
        self.assertIsNone(rewrite_resolved("https://g.oginstagram.com/p/AbC123/"))
        self.assertIsNone(pick_source("fixupx.com"))

    def test_unknown_hosts(self) -> None:
        self.assertIsNone(rewrite_resolved("https://youtube.com/watch?v=abc"))
        self.assertIsNone(rewrite_resolved("https://github.com/foo/bar"))


class TrackingAndApplyTests(unittest.TestCase):
    def test_strips_instagram_and_utm_params(self) -> None:
        url = "https://instagram.com/p/AbC123/?igsh=deadbeef&utm_source=ig&foo=keep"
        cleaned = strip_tracking(url)
        self.assertNotIn("igsh", cleaned)
        self.assertNotIn("utm_source", cleaned)
        self.assertIn("foo=keep", cleaned)

    def test_rewrite_strips_tracking(self) -> None:
        self.assertEqual(
            rewrite_resolved("https://instagram.com/p/AbC123/?igsh=xyz&utm_medium=share"),
            "https://oginstagram.com/p/AbC123/",
        )
        self.assertEqual(
            rewrite_resolved("https://x.com/user/status/123?s=20&t=abc"),
            "https://fixupx.com/user/status/123",
        )

    def test_apply_rewrites_keeps_surrounding_text(self) -> None:
        text = "check this https://x.com/user/status/1 lol"
        out = apply_rewrites(text, [("https://x.com/user/status/1", "https://fixupx.com/user/status/1")])
        self.assertEqual(out, "check this https://fixupx.com/user/status/1 lol")

    def test_apply_rewrites_longest_first(self) -> None:
        text = "https://x.com/user/status/12 and https://x.com/user/status/1"
        out = apply_rewrites(
            text,
            [
                ("https://x.com/user/status/1", "https://fixupx.com/user/status/1"),
                ("https://x.com/user/status/12", "https://fixupx.com/user/status/12"),
            ],
        )
        self.assertEqual(
            out,
            "https://fixupx.com/user/status/12 and https://fixupx.com/user/status/1",
        )


class ResolveTests(unittest.TestCase):
    def test_needs_resolve_share_and_tiktok_shorts(self) -> None:
        self.assertTrue(needs_resolve("https://www.instagram.com/share/reel/AbC"))
        self.assertTrue(needs_resolve("https://vm.tiktok.com/ZSabcde/"))
        self.assertTrue(needs_resolve("https://vt.tiktok.com/ZSabcde/"))
        self.assertFalse(needs_resolve("https://instagram.com/reel/AbC123/"))
        self.assertFalse(needs_resolve("https://www.tiktok.com/@user/video/123"))

    def test_rewrite_one_resolves_instagram_share(self) -> None:
        session = FakeSession("https://www.instagram.com/reel/Canonical/")
        result = asyncio.run(
            rewrite_one("https://www.instagram.com/share/reel/AbC", session)  # type: ignore[arg-type]
        )
        self.assertEqual(result, "https://oginstagram.com/reel/Canonical/")
        self.assertEqual(session.heads, 1)

    def test_rewrite_one_resolves_tiktok_short(self) -> None:
        session = FakeSession("https://www.tiktok.com/@user/video/999")
        result = asyncio.run(
            rewrite_one("https://vm.tiktok.com/ZSabcde/", session)  # type: ignore[arg-type]
        )
        self.assertEqual(result, "https://vxtiktok.com/@user/video/999")


class ConfigTests(unittest.TestCase):
    def test_parse_allowed_guilds(self) -> None:
        self.assertIsNone(parse_allowed_guilds(None))
        self.assertIsNone(parse_allowed_guilds(""))
        self.assertEqual(parse_allowed_guilds("1, 2,3"), {1, 2, 3})
        with self.assertRaises(ValueError):
            parse_allowed_guilds("nope")


if __name__ == "__main__":
    unittest.main()
