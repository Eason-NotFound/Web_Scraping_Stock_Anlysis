"""SQLite durability, revision and mode-isolation regression checks."""
import tempfile
from pathlib import Path
import unittest

from newslens.storage import Store


def news(**changes):
    row = {"id": "news-1", "headline": "Apple earnings increase",
           "summary": "Apple reports higher earnings.", "symbols": ["AAPL"],
           "url": "https://example.com/news", "source": "fixture",
           "created_at": "2026-11-04T15:00:00+00:00",
           "updated_at": "2026-11-04T15:00:00+00:00",
           "received_at": "2026-11-04T15:01:00+00:00", "data_mode": "demo"}
    row.update(changes)
    return row


def quote(**changes):
    row = {"symbol": "AAPL", "price": 100.0, "previous_close": 99.0,
           "source_time": "2026-11-04T15:00:00+00:00",
           "received_at": "2026-11-04T15:00:01+00:00", "feed": "fixture",
           "market_open": True, "data_mode": "demo"}
    row.update(changes)
    return row


def rule(**changes):
    row = {"symbol": "AAPL", "direction": "above", "threshold": 101,
           "cooldown_seconds": 1800, "enabled": True,
           "require_news": False, "categories": ["Finance"]}
    row.update(changes)
    return row


class StorageVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.sqlite"
        self.store = Store(self.path)
        self.store.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_duplicate_news_preserves_original_received_time_and_analysis(self):
        original_hash = self.store.save_news(news())
        analysis = {"status": "success", "summary": "Reviewed original."}
        self.store.save_analysis("news-1", original_hash, analysis)
        duplicate_hash = self.store.save_news(news(received_at="2026-11-04T17:00:00+00:00"))
        self.assertEqual(original_hash, duplicate_hash)
        self.assertEqual(self.store.counts()["news_versions"], 1)
        row = self.store.news_rows()[0]
        self.assertEqual(row["received_at"], "2026-11-04T15:01:00+00:00")
        self.assertEqual(row["analysis"], analysis)

    def test_revision_retains_original_payload_and_analysis(self):
        h1 = self.store.save_news(news())
        self.store.save_analysis("news-1", h1, {"status": "success", "summary": "Original result"})
        h2 = self.store.save_news(news(summary="Apple revises its earnings statement.",
                                       updated_at="2026-11-04T16:00:00+00:00"))
        self.assertNotEqual(h1, h2)
        self.assertEqual(self.store.counts()["news_versions"], 2)
        latest = self.store.news_rows()[0]
        self.assertEqual(latest["hash"], h2)
        self.assertIsNone(latest["analysis"])
        with self.store.connect() as connection:
            original = connection.execute("SELECT payload,analysis FROM news_versions WHERE hash=?", (h1,)).fetchone()
        self.assertIn("Apple reports higher earnings.", original["payload"])
        self.assertIn("Original result", original["analysis"])

    def test_late_arriving_old_revision_does_not_replace_latest(self):
        latest_hash = self.store.save_news(news(updated_at="2026-11-04T16:00:00+00:00"))
        self.store.save_news(news(summary="Older content", updated_at="2026-11-04T14:00:00+00:00"))
        self.assertEqual(self.store.news_rows()[0]["hash"], latest_hash)

    def test_pending_retries_errors_but_not_success_or_insufficient(self):
        h = self.store.save_news(news())
        self.assertEqual(len(self.store.pending_news()), 1)
        self.store.save_analysis("news-1", h, {"status": "error", "error": "Transient API failure"})
        self.assertEqual(len(self.store.pending_news()), 1)
        self.store.save_analysis("news-1", h, {"status": "insufficient"})
        self.assertEqual(self.store.pending_news(), [])
        self.store.save_analysis("news-1", h, {"status": "success"})
        self.assertEqual(self.store.pending_news(), [])

    def test_old_revision_error_is_not_retried_after_successful_new_revision(self):
        h1 = self.store.save_news(news())
        self.store.save_analysis("news-1", h1, {"status": "error"})
        h2 = self.store.save_news(news(summary="New content", updated_at="2026-11-04T16:00:00+00:00"))
        self.store.save_analysis("news-1", h2, {"status": "success"})
        self.assertEqual(self.store.pending_news(), [])

    def test_database_rejects_cross_mode_news_and_quotes(self):
        self.store.save_quote(quote())
        with self.assertRaisesRegex(ValueError, "mode mismatch"):
            self.store.save_quote(quote(data_mode="live", source_time="2026-11-04T15:01:00+00:00"))
        with self.assertRaisesRegex(ValueError, "mode mismatch"):
            self.store.save_news(news(data_mode="live"))
        self.assertEqual(self.store.counts()["quotes"], 1)
        self.assertEqual(self.store.counts()["news_versions"], 0)

    def test_mode_guard_survives_reopening_store(self):
        self.store.assert_mode("demo")
        reopened = Store(self.path)
        reopened.init()
        with self.assertRaisesRegex(ValueError, "mode mismatch"):
            reopened.assert_mode("live")

    def test_rule_update_clears_baseline_and_preserves_id(self):
        rid = self.store.upsert_rule(rule())
        self.store.put_state(f"baseline:{rid}", quote())
        updated_id = self.store.upsert_rule(rule(id=rid, threshold=105))
        self.assertEqual(updated_id, rid)
        self.assertIsNone(self.store.get_state(f"baseline:{rid}"))
        self.assertEqual(self.store.rules()[0]["threshold"], 105)

    def test_disabled_rule_clears_baseline(self):
        rid = self.store.upsert_rule(rule())
        self.store.put_state(f"baseline:{rid}", quote())
        self.store.disable_rule(rid)
        self.assertFalse(self.store.rules()[0]["enabled"])
        self.assertIsNone(self.store.get_state(f"baseline:{rid}"))

    def test_delete_rule_clears_baseline_and_cooldown(self):
        rid = self.store.upsert_rule(rule())
        self.store.put_state(f"baseline:{rid}", quote())
        self.store.put_state(f"cooldown:{rid}", "2026-11-04T16:00:00+00:00")
        self.store.delete_rule(rid)
        self.assertEqual(self.store.rules(), [])
        self.assertIsNone(self.store.get_state(f"baseline:{rid}"))
        self.assertIsNone(self.store.get_state(f"cooldown:{rid}"))

    def test_event_id_deduplicates_and_delivery_state_survives_restart(self):
        event = {"event_id": "event-1", "created_at": "2026-11-04T16:00:00+00:00",
                 "data_mode": "demo", "symbol": "AAPL", "price": 100}
        self.assertTrue(self.store.create_alert(event))
        self.assertFalse(self.store.create_alert(event))
        self.store.set_delivery("event-1", "sent")
        reopened = Store(self.path)
        reopened.init()
        self.assertEqual(len(reopened.alerts()), 1)
        self.assertEqual(reopened.alerts()[0]["delivery_status"], "sent")
        self.assertEqual(reopened.pending_alerts(), [])


if __name__ == "__main__":
    unittest.main()
