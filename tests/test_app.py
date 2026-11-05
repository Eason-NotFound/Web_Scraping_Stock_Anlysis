"""No-network UI checks, isolated from the project's saved databases."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / "app.py"


@unittest.skipUnless(importlib.util.find_spec("newslens.demo"), "Demo module not yet available")
class AppSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environ = patch.dict(os.environ, {
            "NEWSLENS_MODE": "demo", "NEWSLENS_DB": str(Path(self.temp.name) / "demo.sqlite"),
            "NEWSLENS_ADMIN_TOKEN": "", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "",
        })
        self.environ.start()

    def tearDown(self):
        self.environ.stop()
        self.temp.cleanup()

    def app(self):
        at = AppTest.from_file(str(APP), default_timeout=30).run()
        self.assertEqual(len(at.exception), 0)
        return at

    def test_market_news_and_alert_pages_render(self):
        at = self.app()
        self.assertTrue(any("SYNTHETIC REPLAY" in w.value for w in at.warning))
        self.assertGreater(len(at.dataframe), 0)
        for page in ["News Intelligence", "Alerts & Validation", "Market Monitor"]:
            at.radio(key="workspace").set_value(page).run()
            self.assertEqual(len(at.exception), 0, page)

    def test_live_mode_is_empty_and_read_only_without_admin(self):
        at = self.app()
        at.selectbox(key="data_mode").set_value("Live monitoring").run()
        self.assertEqual(len(at.exception), 0)
        self.assertTrue(any("No live quotes yet" in i.value for i in at.info))
        at.radio(key="workspace").set_value("Alerts & Validation").run()
        self.assertEqual(len(at.exception), 0)
        save = next(b for b in at.button if b.label == "Save rule")
        self.assertTrue(save.disabled)
        self.assertEqual(os.environ["NEWSLENS_MODE"], "demo")

    def test_demo_can_create_a_rule_and_advance(self):
        at = self.app()
        at.radio(key="workspace").set_value("Alerts & Validation").run()
        next(b for b in at.button if b.label == "Save rule").click().run()
        self.assertEqual(len(at.exception), 0)
        next(b for b in at.button if b.label == "Advance replay →").click().run()
        next(b for b in at.button if b.label == "Advance replay →").click().run()
        self.assertEqual(len(at.exception), 0)
        from newslens.storage import Store
        store = Store(os.environ["NEWSLENS_DB"])
        self.assertTrue(any(a["kind"] == "composite" for a in store.alerts()))
        before = len(store.alerts())
        at.run()
        self.assertEqual(len(store.alerts()), before)
        self.assertTrue(any("Replay clock" in c.value for c in at.caption))


if __name__ == "__main__":
    unittest.main()
