"""Gateway history fetch: the newest turn is never lost, ids/timestamps ride along, ceilings are transport-only."""
import asyncio
import unittest
from datetime import datetime, timezone

from config import config
from routers import chat as chat_module


class _Pool:
    """Returns canned rows newest-first, honouring LIMIT, like the real SELECT ... ORDER BY DESC."""

    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    async def fetch(self, query, *args):
        self.queries.append((" ".join(query.split()), args))
        return self.rows[: args[1]]


def _row(i, role, content):
    return {"id": i, "role": role, "content": content,
            "created_at": datetime(2026, 10, 23, 0, i, tzinfo=timezone.utc)}


def _history(rows, **kw):
    pool = _Pool(rows)
    out = asyncio.run(chat_module._get_conversation_history(pool, "conv-1", **kw))
    return pool, out


class TestHistoryWindow(unittest.TestCase):
    def test_oldest_first_with_ids_and_timestamps(self):
        _pool, out = _history([_row(3, "user", "c"), _row(2, "assistant", "b"), _row(1, "user", "a")],
                              limit=10, max_chars=1_000)
        self.assertEqual([m["content"] for m in out], ["a", "b", "c"])
        self.assertEqual(out[0]["id"], "1")
        self.assertEqual(out[0]["created_at"], "2026-10-23T00:01:00+00:00")
        self.assertEqual(out[0]["role"], "user")

    def test_query_selects_the_message_id(self):
        pool, _out = _history([_row(1, "user", "a")], limit=10, max_chars=1_000)
        self.assertIn("select id, role, content, created_at", pool.queries[0][0].lower())

    def test_newest_message_over_the_cap_is_kept_not_the_whole_history_dropped(self):
        rows = [_row(2, "assistant", "B" * 50_000), _row(1, "user", "question")]
        _pool, out = _history(rows, limit=10, max_chars=8_192)
        self.assertEqual(len(out), 1)                              # used to be [] -> amnesia
        self.assertEqual(out[0]["id"], "2")
        self.assertIn("characters omitted", out[0]["content"])
        self.assertLessEqual(len(out[0]["content"]), 8_192 + 200)

    def test_older_rows_past_the_cap_are_dropped_oldest_first(self):
        rows = [_row(4, "user", "d" * 400), _row(3, "assistant", "c" * 400),
                _row(2, "user", "b" * 400), _row(1, "assistant", "a" * 400)]
        _pool, out = _history(rows, limit=10, max_chars=900)
        self.assertEqual([m["id"] for m in out], ["3", "4"])

    def test_limit_is_respected(self):
        rows = [_row(i, "user", "x") for i in range(10, 0, -1)]
        _pool, out = _history(rows, limit=3, max_chars=1_000)
        self.assertEqual([m["id"] for m in out], ["8", "9", "10"])

    def test_rows_without_id_or_timestamp_still_work(self):
        _pool, out = _history([{"role": "user", "content": "plain"}], limit=5, max_chars=100)
        self.assertEqual(out, [{"role": "user", "content": "plain"}])

    def test_no_pool_returns_empty(self):
        self.assertEqual(asyncio.run(chat_module._get_conversation_history(None, "c", limit=5, max_chars=5)), [])

    def test_default_ceilings_are_transport_limits_not_the_old_2k_token_window(self):
        self.assertGreaterEqual(config.HISTORY_MESSAGE_COUNT, 1_000)
        self.assertGreaterEqual(config.HISTORY_MAX_CHARS, 1_000_000)


if __name__ == "__main__":
    unittest.main()
