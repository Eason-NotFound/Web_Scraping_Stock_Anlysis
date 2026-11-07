import copy
from functools import partial
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from newslens.storage import Store
from run_practice import PracticeHandler, export_snapshot, practice_response, validate_snapshot


def example():
    return {"schema": 1, "kind": "saved-live-snapshot", "captured_at": "2026-11-07T03:00:00+00:00",
        "status": {"data_available": True, "worker_running": False},
        "quotes": [{"symbol": "AAPL", "price": 100, "data_mode": "live"}],
        "bars": {"AAPL": [{"time": "2026-11-05T00:00:00Z", "close": 100}]},
        "news": [{"id": "test-1", "symbols": ["AAPL", "MSFT"], "analysis": {"categories": ["Finance"], "impacts": [{"symbol": "AAPL", "sentiment": "neutral"}, {"symbol": "MSFT", "sentiment": "positive"}]}},
                 {"id": "test-2", "symbols": ["AAPL"], "analysis": None}],
        "alerts": [{"event_id": "test-event", "symbol": "AAPL", "delivery_status": "pending"}]}


class PracticeTests(unittest.TestCase):
    def test_snapshot_rejects_running_monitor_or_wrong_kind(self):
        data = example()
        self.assertEqual(validate_snapshot(data), data)
        for invalid in [None, {}, {**data, "kind": "live-feed"}, {**data, "status": {"worker_running": True}}]:
            with self.assertRaises(ValueError):
                validate_snapshot(invalid)

    def test_news_filters_match_the_same_stock_impact_and_preserve_snapshot(self):
        data = example()
        original = copy.deepcopy(data)
        self.assertEqual(practice_response(data, "/api/news?symbol=AAPL&sentiment=positive")["data"], [])
        rows = practice_response(data, "/api/news?symbol=MSFT&sentiment=positive&category=Finance")["data"]
        self.assertEqual([r["id"] for r in rows], ["test-1"])
        self.assertEqual(data, original)

    def test_pagination_and_empty_results_follow_the_public_contract(self):
        data = example()
        response = practice_response(data, "/api/news?limit=1&offset=1")
        self.assertEqual(response["meta"]["total"], 2)
        self.assertEqual(response["data"][0]["id"], "test-2")
        self.assertTrue(response["meta"]["practice"])
        self.assertEqual(response["meta"]["snapshot_at"], data["captured_at"])
        self.assertEqual(practice_response(data, "/api/bars?symbol=MSFT")["data"], [])
        self.assertEqual(practice_response(data, "/api/alerts?symbol=MSFT")["meta"]["total"], 0)

    def test_unsupported_queries_and_hidden_routes_are_rejected(self):
        for path in ["/api/news?limit=99999", "/api/news?offset=-1", "/api/news?sentiment=invented", "/api/news?symbol=AAPL&symbol=MSFT", "/api/news?token=secret", "/api/bars"]:
            with self.assertRaises(ValueError):
                practice_response(example(), path)
        with self.assertRaises(KeyError):
            practice_response(example(), "/api/rules")

    def test_export_uses_read_only_local_data_with_all_network_connections_disabled(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "live.sqlite"
            store = Store(db)
            store.init()
            store.assert_mode("live")
            target = Path(folder) / "practice.json"
            before = db.read_bytes()
            with patch("socket.socket.connect", side_effect=AssertionError("Network disabled")):
                result = export_snapshot(db, target)
            self.assertFalse(result["status"]["worker_running"])
            self.assertEqual(db.read_bytes(), before)
            self.assertEqual(json.loads(target.read_text())["kind"], "saved-live-snapshot")
            self.assertEqual(target.stat().st_mode & 0o077, 0)

    def test_export_refuses_running_worker_before_saving_a_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "live.sqlite"
            store = Store(db)
            store.init()
            store.assert_mode("live")
            import time
            from newslens.storage import utcnow
            store.acquire_lease("test-worker", time.time(), ttl=1800)
            store.put_state("worker_last_seen", utcnow())
            target = Path(folder) / "practice.json"
            with self.assertRaisesRegex(ValueError, "Pause"):
                export_snapshot(db, target)
            self.assertFalse(target.exists())


class PracticeHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(PracticeHandler, snapshot=example()))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, method, path):
        c = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        c.request(method, path)
        r = c.getresponse()
        result = r.status, dict(r.getheaders()), r.read()
        c.close()
        return result

    def test_page_has_persistent_snapshot_notice_and_original_local_assets(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])
        self.assertIn(b"OFFLINE PRACTICE", body)
        self.assertIn(b"No Alpaca or HKBU requests", body)
        self.assertIn(b"/app.js", body)
        self.assertIn(b"/practice.css", body)
        for path in ["/app.js", "/styles.css", "/practice.css", "/api/status", "/api/news", "/api/alerts"]:
            self.assertEqual(self.request("GET", path)[0], 200)

    def test_private_files_and_writes_are_never_served(self):
        for path in ["/.env", "/data/live.sqlite", "/../.env", "/%2e%2e/.env", "/api/rules"]:
            self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("POST", "/api/news")[0], 405)
        self.assertEqual(self.request("GET", "/api/news?limit=0")[0], 400)
        self.assertEqual(self.request("HEAD", "/")[2], b"")
