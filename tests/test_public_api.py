"""Public API contract regressions using isolated databases and synthetic secrets."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from newslens.api import create_app
from newslens.storage import Store

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "synthetic-api-test-token-not-a-real-credential"
BASE_TIME = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)


def stamp(minutes=0):
    return (BASE_TIME + timedelta(minutes=minutes)).isoformat()


class PublicApiTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "work" / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = TemporaryDirectory(prefix="public-api-test-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "live.sqlite"
        self.env = patch.dict(os.environ, {"NEWSLENS_ENABLE_TELEGRAM": "0"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.transport = patch("newslens.data_provider.urlopen", side_effect=AssertionError("Public API must only read saved data"))
        self.network = self.transport.start()
        self.addCleanup(self.transport.stop)

    def client(self, token=TOKEN, authenticated=True, path=None):
        client = TestClient(create_app(db_path=path or self.path, api_token=token, symbols=["AAPL", "MSFT"]))
        if authenticated:
            client.headers["Authorization"] = "Bearer " + TOKEN
        self.addCleanup(client.close)
        return client

    def store(self, mode="live"):
        store = Store(self.path)
        store.init()
        store.assert_mode(mode)
        return store

    def news(self, store, article_id="article-1", updated=0, symbols=None, **changes):
        item = {"id": article_id, "headline": "Apple earnings update", "summary": "Apple revenue increased.",
                "symbols": symbols or ["AAPL"], "created_at": stamp(), "updated_at": stamp(updated),
                "received_at": stamp(updated + 1), "source": "Offline fixture", "url": "https://example.com/article",
                "data_mode": "live"}
        item.update(changes)
        version = store.save_news(item)
        return item, version

    def analysis(self, store, item, version, category="Finance", sentiment="positive", impacts=None, **changes):
        value = {"status": "success", "categories": [category], "summary": "Revenue increased.",
                 "impacts": impacts if impacts is not None else [{"symbol": "AAPL", "sentiment": sentiment,
                     "reason": "Reported revenue increased.", "evidence": "Apple revenue increased."}],
                 "model": "offline-fixture", "prompt_version": "test-v1", "analyzed_at": stamp(10), "error": ""}
        value.update(changes)
        store.save_analysis(item["id"], version, value)

    def quote(self, store, symbol="AAPL", source_time=None, **changes):
        value = {"symbol": symbol, "price": 102.0, "previous_close": 100.0,
                 "source_time": source_time or stamp(), "received_at": stamp(1),
                 "market_open": True, "clock_status": "verified", "feed": "iex", "data_mode": "live"}
        value.update(changes)
        store.save_quote(value)

    def snapshot(self, store):
        with store.connect() as conn:
            return list(conn.iterdump())

    def assert_error(self, response, status, code=None):
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertIn("error", body)
        if code:
            self.assertEqual(body["error"]["code"], code)
        self.assertNotIn(TOKEN, response.text)
        self.assertNotIn(str(self.root), response.text)

    def test_authentication_fails_closed_before_any_database_creation(self):
        client = self.client(authenticated=False)
        for headers in ({}, {"Authorization": "Bearer wrong-token"}, {"Authorization": "Basic invalid"},
                        [(b"authorization", b"Bearer \xff")]):
            with self.subTest(headers_kind=type(headers).__name__):
                self.assert_error(client.get("/api/status", headers=headers), 401)
        self.assertFalse(self.path.exists())
        self.network.assert_not_called()

    def test_unconfigured_origin_token_is_unavailable(self):
        for token in (None, ""):
            with self.subTest(token=token):
                client = self.client(token=token)
                self.assert_error(client.get("/api/status"), 503, "api_not_configured")
        self.assertFalse(self.path.exists())

    def test_missing_database_has_honest_status_and_is_never_created(self):
        client = self.client()
        response = client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["data"]["data_available"])
        self.assertFalse(body["data"]["worker_running"])
        self.assertEqual(body["meta"]["data_mode"], "live")
        for path in ("/api/quotes", "/api/news", "/api/bars?symbol=AAPL", "/api/alerts"):
            self.assert_error(client.get(path), 503, "data_unavailable")
        self.assertFalse(self.path.exists())

    def test_demo_database_is_never_exposed_as_live(self):
        store = self.store(mode="demo")
        before = self.snapshot(store)
        client = self.client()
        self.assertFalse(client.get("/api/status").json()["data"]["data_available"])
        for path in ("/api/quotes", "/api/news", "/api/bars?symbol=AAPL", "/api/alerts"):
            self.assert_error(client.get(path), 503, "data_unavailable")
        self.assertEqual(self.snapshot(store), before)

    def test_all_reads_preserve_database_and_project_only_public_fields(self):
        store = self.store()
        private_marker = "private-marker-never-in-public-response"
        self.quote(store, internal_key=private_marker)
        item, version = self.news(store, internal_key=private_marker)
        impact = {"symbol": "AAPL", "sentiment": "positive", "reason": "Revenue increased.",
                  "evidence": "Apple revenue increased.", "api_key": private_marker}
        self.analysis(store, item, version, impacts=[impact], internal_key=private_marker,
                      error="Private exception: " + private_marker)
        store.save_bars([{"symbol": "AAPL", "time": stamp(), "close": 100.0, "private_path": private_marker}])
        store.create_alert({"event_id": "event-1", "created_at": stamp(), "data_mode": "live", "symbol": "AAPL",
                            "kind": "news", "price": None, "reason": "Revenue increased.", "private_path": private_marker})
        store.put_state("internal_secret", private_marker)
        store.log("ERROR", private_marker)
        before = self.snapshot(store)
        client = self.client()
        for path in ("/api/status", "/api/quotes", "/api/news", "/api/bars?symbol=AAPL", "/api/alerts"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertIn("data", body)
                self.assertEqual(body["meta"]["data_mode"], "live")
                self.assertIn("generated_at", body["meta"])
                self.assertNotIn(private_marker, response.text)
                self.assertNotIn(TOKEN, response.text)
                self.assertNotIn(str(self.root), response.text)
        self.assertEqual(self.snapshot(store), before)
        self.network.assert_not_called()

    def test_news_selects_latest_version_before_filtering_and_paging(self):
        store = self.store()
        old, h1 = self.news(store, "revision", updated=1)
        self.analysis(store, old, h1, category="Finance", sentiment="positive")
        latest, h2 = self.news(store, "revision", updated=3, summary="Apple revised its outlook.")
        self.analysis(store, latest, h2, category="Politics & Policy", sentiment="negative")
        other, other_hash = self.news(store, "other", updated=2)
        self.analysis(store, other, other_hash, category="Finance", sentiment="positive")
        client = self.client()
        latest_rows = client.get("/api/news").json()
        self.assertEqual(latest_rows["meta"]["total"], 2)
        self.assertEqual(latest_rows["data"][0]["id"], "revision")
        self.assertEqual(latest_rows["data"][0]["hash"], h2)
        filtered = client.get("/api/news", params={"category": "Finance", "limit": 1, "offset": 0}).json()
        self.assertEqual(filtered["meta"]["total"], 1)
        self.assertEqual([item["id"] for item in filtered["data"]], ["other"])
        empty = client.get("/api/news", params={"category": "Finance", "limit": 1, "offset": 1}).json()
        self.assertEqual(empty["data"], [])
        self.assertEqual(empty["meta"]["total"], 1)

    def test_tied_news_timestamps_use_latest_inserted_revision(self):
        store = self.store()
        self.news(store, "revision", updated=2, summary="First version.")
        _, newest = self.news(store, "revision", updated=2, summary="Second version.")
        result = self.client().get("/api/news").json()
        self.assertEqual(len(result["data"]), 1)
        self.assertEqual(result["data"][0]["hash"], newest)
        self.assertEqual(result["data"][0]["summary"], "Second version.")

    def test_sentiment_filter_applies_to_the_selected_company(self):
        store = self.store()
        item, version = self.news(store, symbols=["AAPL", "MSFT"])
        impacts = [{"symbol": "AAPL", "sentiment": "negative", "reason": "Apple outlook weak.", "evidence": "Apple"},
                   {"symbol": "MSFT", "sentiment": "positive", "reason": "Microsoft outlook strong.", "evidence": "Microsoft"}]
        self.analysis(store, item, version, impacts=impacts)
        client = self.client()
        self.assertEqual(client.get("/api/news", params={"symbol": "AAPL", "sentiment": "positive"}).json()["data"], [])
        self.assertEqual(len(client.get("/api/news", params={"symbol": "MSFT", "sentiment": "positive"}).json()["data"]), 1)
        self.assertEqual(len(client.get("/api/news", params={"sentiment": "positive"}).json()["data"]), 1)

    def test_bars_return_newest_window_in_chronological_order(self):
        store = self.store()
        store.save_bars([{"symbol": "AAPL", "time": stamp(i), "close": 100.0 + i} for i in range(5)]
                        + [{"symbol": "MSFT", "time": stamp(10), "close": 200.0}])
        response = self.client().get("/api/bars", params={"symbol": "AAPL", "limit": 2})
        self.assertEqual(response.status_code, 200)
        bars = response.json()["data"]
        self.assertEqual([row["time"] for row in bars], [stamp(3), stamp(4)])
        self.assertEqual([row["close"] for row in bars], [103.0, 104.0])

    def test_alerts_use_current_delivery_column_and_filter_before_paging(self):
        store = self.store()
        for i, symbol in enumerate(("AAPL", "MSFT", "AAPL")):
            store.create_alert({"event_id": f"event-{i}", "created_at": stamp(i), "data_mode": "live", "symbol": symbol,
                                "kind": "news", "price": None, "reason": "Fixture alert."})
        store.set_delivery("event-2", "sent")
        body = self.client().get("/api/alerts", params={"symbol": "AAPL", "limit": 1, "offset": 0}).json()
        self.assertEqual(body["meta"]["total"], 2)
        self.assertEqual(body["data"][0]["delivery_status"], "sent")
        self.assertEqual(body["data"][0]["created_at"], stamp(2))

    def test_running_requires_valid_lease_and_recent_nonfuture_heartbeat(self):
        store = self.store()
        client = self.client()
        now = datetime.now(timezone.utc)
        store.acquire_lease("fixture-worker", time.time(), ttl=1800)
        for delta, expected in ((0, True), (-300, False), (3600, False)):
            with self.subTest(delta=delta):
                store.put_state("worker_last_seen", (now + timedelta(seconds=delta)).isoformat())
                self.assertEqual(client.get("/api/status").json()["data"]["worker_running"], expected)
        store.put_state("worker_last_seen", now.isoformat())
        store.release_lease("fixture-worker")
        self.assertFalse(client.get("/api/status").json()["data"]["worker_running"])

    def test_invalid_queries_and_write_routes_are_rejected_safely(self):
        self.store()
        client = self.client()
        for path, params in (("/api/news", {"limit": 0}), ("/api/news", {"limit": 101}),
                             ("/api/news", {"offset": -1}), ("/api/news", {"offset": 10001}),
                             ("/api/news", {"category": "not-a-category"}), ("/api/news", {"sentiment": "invented"}),
                             ("/api/bars", {"symbol": "AAPL'; DROP TABLE news_versions;--"}),
                             ("/api/bars", {"symbol": "AAPL", "limit": 501})):
            with self.subTest(path=path, params=params):
                self.assert_error(client.get(path, params=params), 422, "invalid_request")
        self.assert_error(client.post("/api/news", json={"secret": TOKEN}), 405)
        for path in ("/api/rules", "/api/logs", "/api/stop", "/docs", "/openapi.json"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 404)
        self.network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
