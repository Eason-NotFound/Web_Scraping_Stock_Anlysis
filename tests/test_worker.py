from datetime import timedelta
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch
from newslens.storage import Store
from newslens.demo import seed_demo,advance_demo,ANCHOR
from newslens.worker import process_quotes,process_news_alerts
from newslens.notifications import deliver_pending

class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.s=Store(Path(self.tmp.name)/"demo.sqlite")
        seed_demo(self.s)
    def tearDown(self): self.tmp.cleanup()
    def test_replay_crossing_and_restart_dedup(self):
        self.assertEqual(len(self.s.latest_quotes()),10)
        initial=len(self.s.alerts()); advance_demo(self.s); advance_demo(self.s)
        alerts=self.s.alerts(); self.assertEqual(len(alerts),initial+1)
        self.assertEqual(alerts[0]["kind"],"composite")
        s2=Store(self.s.path); q=s2.latest_quotes()
        process_quotes(s2,q,ANCHOR+timedelta(minutes=2))
        self.assertEqual(len(s2.alerts()),len(alerts))
        self.assertEqual(process_news_alerts(s2,ANCHOR+timedelta(minutes=2)),0)
    def test_no_default_external_send(self):
        with patch.dict(os.environ,{"NEWSLENS_ENABLE_TELEGRAM":"0"}):
            with patch("newslens.notifications.urlopen") as send:
                self.assertEqual(deliver_pending(self.s),0); send.assert_not_called()
    def test_demo_never_sent_even_when_enabled(self):
        with patch.dict(os.environ,{"NEWSLENS_ENABLE_TELEGRAM":"1","TELEGRAM_BOT_TOKEN":"not-real","TELEGRAM_CHAT_ID":"not-real"}):
            with patch("newslens.notifications.urlopen") as send:
                self.assertEqual(deliver_pending(self.s),0); send.assert_not_called()
        self.assertTrue(all(a["delivery_status"]=="skipped_demo" for a in self.s.alerts()))
    def test_live_send_timeout_is_not_retried(self):
        live=Store(Path(self.tmp.name)/"live.sqlite"); live.init()
        live.create_alert(dict(event_id="x",symbol="AAPL",kind="price",created_at=ANCHOR.isoformat(),price=10,reason="test",data_mode="live"))
        with patch.dict(os.environ,{"NEWSLENS_ENABLE_TELEGRAM":"1","TELEGRAM_BOT_TOKEN":"not-real","TELEGRAM_CHAT_ID":"not-real"}):
            with patch("newslens.notifications.urlopen",side_effect=TimeoutError) as send:
                deliver_pending(live); deliver_pending(live)
                self.assertEqual(send.call_count,1)
        self.assertEqual(live.alerts()[0]["delivery_status"],"unknown")

if __name__=="__main__": unittest.main()
