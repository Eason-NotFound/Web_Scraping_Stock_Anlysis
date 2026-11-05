import tempfile
import unittest
from pathlib import Path
from newslens.storage import Store

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.s=Store(Path(self.tmp.name)/"test.sqlite"); self.s.init()
    def tearDown(self): self.tmp.cleanup()
    def test_mode_isolation(self):
        self.s.assert_mode("demo")
        with self.assertRaises(ValueError): self.s.assert_mode("live")
    def test_alert_idempotency_persists_restart(self):
        a=dict(event_id="same",created_at="2026-01-01T00:00:00+00:00",data_mode="demo")
        self.assertTrue(self.s.create_alert(a)); self.assertFalse(Store(self.s.path).create_alert(a))
        self.s.set_delivery("same","sent"); self.assertEqual(self.s.pending_alerts(),[])
    def test_lease_and_recovery(self):
        self.assertTrue(self.s.acquire_lease("a",100,10)); self.assertFalse(self.s.acquire_lease("b",105,10))
        self.assertTrue(self.s.acquire_lease("b",111,10)); self.s.release_lease("a")
        self.assertFalse(self.s.acquire_lease("a",112,10)); self.s.release_lease("b")
        self.assertTrue(self.s.acquire_lease("a",113,10))
    def test_rule_rejects_nan_and_injection(self):
        with self.assertRaises(ValueError): self.s.upsert_rule(dict(symbol="AAPL",direction="above",threshold=float("nan")))
        with self.assertRaises(ValueError): self.s.upsert_rule(dict(symbol="'; DROP TABLE rules;",direction="above",threshold=10))
    def test_duplicate_quotes_and_history(self):
        q=dict(symbol="AAPL",price=10,previous_close=9,source_time="2026-01-01T00:00:00+00:00",data_mode="demo")
        self.s.save_quote(q); self.s.save_quote(q)
        self.assertEqual(self.s.counts()["quotes"],1)
        self.assertEqual(self.s.latest_quotes()[0]["price"],10)

if __name__=="__main__": unittest.main()
