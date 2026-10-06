"""Edit and delete audit helpers — no Discord token required."""

from __future__ import annotations

import sys
import unittest
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import (
    MISSING_DELETE_TEXT,
    MISSING_EDIT_TEXT,
    AuditMessage,
    MessageSnapshot,
    SnapshotCache,
    consume_bot_delete,
    filter_bot_deletes,
    format_bulk_delete_embeds,
    format_delete_embed,
    format_edit_embed,
    is_real_edit,
    mark_bot_delete,
    truncate_audit_text,
)


EDITED = "2026-10-06T19:00:00.000000+00:00"


def _entry(**overrides: object) -> AuditMessage:
    data = dict(
        message_id=10,
        channel_id=20,
        author_id=30,
        author_name="Raffie",
        content="hello",
        attachments=(),
    )
    data.update(overrides)
    return AuditMessage(**data)  # type: ignore[arg-type]


class EditDetectionTests(unittest.TestCase):
    def test_embed_unfurl_is_not_an_edit(self) -> None:
        self.assertFalse(is_real_edit(None, "hello", "hello"))
        self.assertFalse(is_real_edit("", "hello", "hello"))

    def test_content_change_without_timestamp_is_not_an_edit(self) -> None:
        self.assertFalse(is_real_edit(None, "hello", "hello world"))

    def test_same_text_with_timestamp_is_not_an_edit(self) -> None:
        self.assertFalse(is_real_edit(EDITED, "hello", "hello"))

    def test_changed_text_with_timestamp_is_an_edit(self) -> None:
        self.assertTrue(is_real_edit(EDITED, "hello", "hello world"))

    def test_unknown_previous_text_still_counts(self) -> None:
        self.assertTrue(is_real_edit(EDITED, None, "hello world"))


class TruncateTests(unittest.TestCase):
    def test_empty_and_short_text(self) -> None:
        self.assertEqual(truncate_audit_text(""), "(no text)")
        self.assertEqual(truncate_audit_text("hello"), "hello")

    def test_long_text_is_cut_to_the_limit(self) -> None:
        shown = truncate_audit_text("a" * 1200, limit=1000)
        self.assertEqual(len(shown), 1000)
        self.assertTrue(shown.endswith("…"))
        self.assertEqual(truncate_audit_text("abcdef", limit=1), "…")


class BotDeleteTests(unittest.TestCase):
    def test_consume_skips_only_ids_the_bot_deleted(self) -> None:
        deleted: OrderedDict[int, None] = OrderedDict()
        mark_bot_delete(deleted, 5)
        self.assertTrue(consume_bot_delete(deleted, 5))
        self.assertFalse(consume_bot_delete(deleted, 5))
        self.assertFalse(consume_bot_delete(deleted, 6))

    def test_oldest_bot_delete_ids_are_dropped(self) -> None:
        deleted: OrderedDict[int, None] = OrderedDict()
        mark_bot_delete(deleted, 1, limit=2)
        mark_bot_delete(deleted, 2, limit=2)
        mark_bot_delete(deleted, 3, limit=2)
        self.assertFalse(consume_bot_delete(deleted, 1))
        self.assertTrue(consume_bot_delete(deleted, 2))
        self.assertTrue(consume_bot_delete(deleted, 3))

    def test_filter_bot_deletes_removes_and_consumes(self) -> None:
        deleted: OrderedDict[int, None] = OrderedDict()
        mark_bot_delete(deleted, 2)
        self.assertEqual(filter_bot_deletes([1, 2, 3], deleted), [1, 3])
        self.assertFalse(consume_bot_delete(deleted, 2))


class SnapshotCacheTests(unittest.TestCase):
    def test_evicts_oldest(self) -> None:
        cache = SnapshotCache(max_size=2)
        first = MessageSnapshot(1, 9, 8, "A", "one")
        second = MessageSnapshot(2, 9, 8, "A", "two")
        third = MessageSnapshot(3, 9, 8, "A", "three")
        cache.remember(first)
        cache.remember(second)
        cache.get(1)
        cache.remember(third)
        self.assertIsNone(cache.pop(2))
        self.assertEqual(cache.pop(1), first)
        self.assertEqual(cache.pop(3), third)


class FormatTests(unittest.TestCase):
    def test_edit_embed_quotes_before_and_after(self) -> None:
        embed = format_edit_embed(
            guild_id=763081865455206450,
            channel_id=42,
            channel_name="general",
            message_id=99,
            author_id=7,
            author_name="Raffie",
            before="hello",
            after="hello world",
        )
        self.assertEqual(embed.title, "Message edited")
        self.assertIn("#general", embed.description or "")
        self.assertIn(
            "https://discord.com/channels/763081865455206450/42/99",
            embed.description or "",
        )
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(fields["Author"], "Raffie (`7`)")
        self.assertEqual(fields["Before"], "hello")
        self.assertEqual(fields["After"], "hello world")
        self.assertNotIn("<@", embed.description or "")
        self.assertNotIn("<@", fields["Author"])

    def test_edit_embed_truncates_and_notes_missing_before(self) -> None:
        embed = format_edit_embed(
            guild_id=1,
            channel_id=2,
            channel_name=None,
            message_id=3,
            author_id=4,
            author_name="Raffie",
            before=None,
            after="x" * 1200,
        )
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(fields["Before"], MISSING_EDIT_TEXT)
        self.assertEqual(len(fields["After"]), 1000)
        self.assertIn("channel `2`", embed.description or "")

    def test_delete_embed_known_and_unknown_text(self) -> None:
        known = format_delete_embed(
            channel_name="general",
            entry=_entry(attachments=("pic.png",)),
        )
        fields = {field.name: field.value for field in known.fields}
        self.assertEqual(known.title, "Message deleted")
        self.assertEqual(fields["Content"], "hello")
        self.assertEqual(fields["Attachments"], "pic.png")
        self.assertEqual(fields["Message ID"], "`10`")

        unknown = format_delete_embed(
            channel_name=None,
            entry=_entry(author_id=None, author_name="Unknown", content=None),
        )
        unknown_fields = {field.name: field.value for field in unknown.fields}
        self.assertEqual(unknown_fields["Content"], MISSING_DELETE_TEXT)
        self.assertNotIn("Attachments", unknown_fields)
        self.assertIn("channel `20`", unknown.description or "")

    def test_bulk_delete_is_one_summary_until_it_needs_another_embed(self) -> None:
        entries = [
            _entry(message_id=1, content="alpha"),
            _entry(message_id=2, author_id=None, author_name="Unknown", content=None),
        ]
        embeds = format_bulk_delete_embeds(
            channel_id=20,
            channel_name="general",
            entries=entries,
        )
        self.assertEqual(len(embeds), 1)
        description = embeds[0].description or ""
        self.assertIn("2 messages removed in #general", description)
        self.assertIn("alpha", description)
        self.assertIn(MISSING_DELETE_TEXT, description)
        self.assertNotIn("<@", description)

        long_entries = [
            _entry(message_id=index, content="word " * 30)
            for index in range(1, 5)
        ]
        split = format_bulk_delete_embeds(
            channel_id=20,
            channel_name="logs",
            entries=long_entries,
            description_limit=80,
        )
        self.assertGreater(len(split), 1)
        self.assertEqual(split[0].title, "Messages deleted")
        self.assertEqual(split[1].title, "Messages deleted (continued)")

    def test_bulk_delete_of_nothing_is_empty(self) -> None:
        self.assertEqual(
            format_bulk_delete_embeds(channel_id=1, channel_name="general", entries=[]),
            [],
        )


if __name__ == "__main__":
    unittest.main()
