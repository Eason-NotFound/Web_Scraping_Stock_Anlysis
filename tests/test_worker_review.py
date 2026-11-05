"""Review regressions: queue fairness and rejected timestamps must stay harmless."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from newslens.signals import news_alert
from newslens.storage import Store
from newslens.worker import process_quotes, run_cycle


class WorkerReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "review.sqlite")
        self.store.init()
        self.store.assert_mode("live")

    def test_retry_exhausted_front_of_queue_does_not_starve_new_work(self):
        now = datetime.now(timezone.utc)
        rows = []
        for i in range(3):
            stamp = (now - timedelta(minutes=i + 1)).isoformat()
            item = {"id": str(i), "headline": f"Apple announcement {i}",
                    "summary": f"Apple provided business information {i}.", "symbols": ["AAPL"],
                    "created_at": stamp, "updated_at": stamp, "received_at": stamp,
                    "data_mode": "live", "source": "fixture", "url": ""}
            item["hash"] = self.store.save_news(item)
            rows.append(item)
        for item in rows[:2]:
            self.store.put_state(f"ai_retry:{item['id']}:{item['hash']}", {"attempts": 3, "after": 0})
        class CountingAnalyzer:
            def __init__(self): self.ids = []
            def analyze(self, item):
                self.ids.append(item["id"])
                return {"status": "insufficient", "categories": [], "impacts": [], "summary": ""}
        analyzer = CountingAnalyzer()
        settings = SimpleNamespace(symbols=["AAPL"], max_age_seconds=180)
        run_cycle(settings, self.store, None, analyzer, prices=False, news=False)
        self.assertEqual(analyzer.ids, ["2"], "Retry filtering must happen before the two-call budget")

    def test_future_quote_does_not_poison_latest_or_rule_baseline(self):
        now = datetime(2026, 11, 4, 15, 1, tzinfo=timezone.utc)
        rid = self.store.upsert_rule({"symbol": "AAPL", "direction": "above", "threshold": 100})
        invalid = {"symbol": "AAPL", "price": 102, "previous_close": 98,
                   "source_time": (now + timedelta(days=1)).isoformat(),
                   "received_at": now.isoformat(), "market_open": True,
                   "feed": "iex", "data_mode": "live"}
        process_quotes(self.store, [invalid], now)
        self.assertEqual(self.store.latest_quotes(), [], "Future data must be rejected before persistence")
        self.assertIsNone(self.store.get_state(f"baseline:{rid}"))

    def test_nested_category_is_rejected_without_breaking_cycle(self):
        now = datetime.now(timezone.utc)
        stamp = (now - timedelta(minutes=1)).isoformat()
        news = {"id": "bad", "headline": "Apple update", "summary": "Apple update.",
                "symbols": ["AAPL"], "created_at": stamp, "updated_at": stamp,
                "received_at": stamp, "data_mode": "live"}
        analysis = {"status": "success", "categories": [["Finance"]], "impacts": []}
        self.assertEqual(news_alert(news, analysis, now), [])


if __name__ == "__main__":
    unittest.main()
