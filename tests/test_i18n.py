"""Offline localization regressions, isolated from real credentials and data."""
from datetime import datetime, timezone
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from newslens.i18n import localize_log, translate
from newslens.storage import Store

ROOT = Path(__file__).resolve().parents[1]
HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


class LocalizationTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "work" / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = TemporaryDirectory(prefix="i18n-test-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = {
            "NEWSLENS_MODE": "demo", "NEWSLENS_DB": str(self.root / "demo.sqlite"),
            "NEWSLENS_ADMIN_TOKEN": "", "NEWSLENS_ENABLE_TELEGRAM": "0",
            "ALPACA_API_KEY": "", "ALPACA_SECRET_KEY": "", "LLM_API_KEY": "",
            "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "",
        }
        self.environ = patch.dict(os.environ, env, clear=True)
        self.environ.start()
        self.addCleanup(self.environ.stop)
        self.secrets = patch("streamlit.secrets", {})
        self.secrets.start()
        self.addCleanup(self.secrets.stop)
        self.transport = patch("newslens.data_provider.urlopen", side_effect=AssertionError("Localization must not use the network"))
        self.network = self.transport.start()
        self.addCleanup(self.transport.stop)
        self.start_worker = patch("newslens.worker.main", side_effect=AssertionError("The UI must not start the worker"))
        self.start_worker.start()
        self.addCleanup(self.start_worker.stop)

    def app(self):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
        self.assertEqual(len(app.exception), 0)
        return app

    def assert_ok(self, app):
        self.assertEqual(len(app.exception), 0)
        self.network.assert_not_called()

    def set_language(self, app, language):
        app.selectbox(key="ui_language").set_value(language).run()
        self.assert_ok(app)
        return app

    def set_page(self, app, page):
        app.radio(key="workspace").set_value(page).run()
        self.assert_ok(app)
        return app

    def assert_browser_selection_sent(self, widget, canonical, language):
        # A correct Python value alone does not update a browser's selected label.
        self.assertTrue(widget.proto.set_value, "A language change must resend the selected label to the browser")
        self.assertEqual(widget.proto.raw_value, translate(canonical, language))

    def chrome(self, app):
        values = []
        for kind in ("title", "header", "subheader", "caption", "info", "warning", "success", "error", "markdown", "text"):
            values.extend(str(item.value) for item in app.get(kind))
        for kind in ("selectbox", "radio", "multiselect", "checkbox", "number_input", "text_input", "button", "expander", "metric"):
            for item in app.get(kind):
                values.append(str(item.label))
                values.append(str(getattr(item.proto, "help", "")))
                if hasattr(item, "options"):
                    values.extend(str(option) for option in item.options)
        for item in app.dataframe:
            frame = item.value
            values.extend(str(value) for value in frame.columns)
            values.extend(str(value) for value in frame.to_numpy().ravel())
        return "\n".join(values)

    def live_store(self):
        path = self.root / "live.sqlite"
        os.environ.update(NEWSLENS_MODE="live", NEWSLENS_DB=str(path))
        store = Store(path)
        store.init()
        store.assert_mode("live")
        return store

    def test_english_is_default_and_demo_chrome_has_no_accidental_chinese(self):
        app = self.app()
        self.assertEqual(app.selectbox(key="ui_language").value, "English")
        for page in ("Market Monitor", "News Intelligence", "Alerts & Validation"):
            with self.subTest(page=page):
                self.set_page(app, page)
                chrome = self.chrome(app).replace("Language / 语言", "Language").replace("中文", "Chinese")
                self.assertIsNone(HAN.search(chrome), "English chrome contains an untranslated Chinese string")
                self.assertTrue(any(item.value == page for item in app.title))

    def test_language_switch_preserves_live_page_and_admin_authorization(self):
        token = "offline-test-admin-token"
        os.environ["NEWSLENS_ADMIN_TOKEN"] = token
        app = self.app()
        app.selectbox(key="data_mode").set_value("Live monitoring").run()
        self.set_page(app, "Alerts & Validation")
        self.assertTrue(next(item for item in app.button if item.label == "Save rule").disabled)
        self.set_language(app, "中文")
        self.assertEqual(app.selectbox(key="data_mode").value, "Live monitoring")
        self.assertEqual(app.radio(key="workspace").value, "Alerts & Validation")
        self.assert_browser_selection_sent(app.selectbox(key="data_mode"), "Live monitoring", "zh")
        self.assert_browser_selection_sent(app.radio(key="workspace"), "Alerts & Validation", "zh")
        self.assertTrue(next(item for item in app.button if item.label == translate("Save rule", "zh")).disabled)
        app.text_input[0].set_value(token).run()
        self.assert_ok(app)
        self.assertFalse(next(item for item in app.button if item.label == translate("Save rule", "zh")).disabled)
        self.set_language(app, "English")
        self.assertEqual(app.radio(key="workspace").value, "Alerts & Validation")
        self.assertEqual(app.selectbox(key="data_mode").value, "Live monitoring")
        self.assert_browser_selection_sent(app.selectbox(key="data_mode"), "Live monitoring", "en")
        self.assert_browser_selection_sent(app.radio(key="workspace"), "Alerts & Validation", "en")
        self.assertFalse(next(item for item in app.button if item.label == "Save rule").disabled)
        self.assertEqual(os.environ["NEWSLENS_MODE"], "demo", "Language or UI mode must not mutate the configured environment")

    def test_chinese_rule_form_persists_canonical_direction_and_categories(self):
        app = self.set_page(self.set_language(self.app(), "中文"), "Alerts & Validation")
        self.assertTrue(HAN.search(next(item for item in app.title if item.value != "◉ NewsLens").value))
        direction = next(item for item in app.selectbox if item.label == translate("Crossing direction", "zh"))
        direction.set_value("below")
        categories = next(item for item in app.multiselect if item.label == translate("Matching news categories (composite rule)", "zh"))
        categories.set_value(["Finance", "Macro"])
        next(item for item in app.button if item.label == translate("Save rule", "zh")).click().run()
        self.assert_ok(app)
        rule = Store(self.root / "demo.sqlite").rules()[-1]
        self.assertEqual(rule["direction"], "below")
        self.assertEqual(rule["categories"], ["Finance", "Macro"])
        self.set_language(app, "English")
        self.assertEqual(app.radio(key="workspace").value, "Alerts & Validation")
        self.assertEqual(Store(self.root / "demo.sqlite").rules()[-1], rule)

    def test_source_news_and_evidence_are_unchanged_in_both_languages(self):
        store = self.live_store()
        stamp = datetime.now(timezone.utc).isoformat()
        headline = "原始标题 Original financial update"
        summary = "原文摘要 Revenue rose by 10%."
        evidence = "Revenue rose by 10%. 原文引用"
        analysis_summary = "模型摘要 Original analysis wording"
        item = {"id": "i18n-fixture", "headline": headline, "summary": summary,
                "symbols": ["AAPL"], "created_at": stamp, "updated_at": stamp,
                "received_at": stamp, "data_mode": "live", "source": "Fixture source", "url": ""}
        version = store.save_news(item)
        store.save_analysis(item["id"], version, {"status": "success", "categories": ["Finance"],
            "summary": analysis_summary, "impacts": [{"symbol": "AAPL", "sentiment": "positive",
            "reason": "原始理由 Original reason", "evidence": evidence}], "model": "offline-fixture",
            "prompt_version": "offline-v1", "analyzed_at": stamp})
        before = store.news_rows()
        app = self.set_page(self.app(), "News Intelligence")
        for language in ("English", "中文", "English"):
            with self.subTest(language=language):
                self.set_language(app, language)
                self.assertTrue(any(headline in item.value for item in app.markdown))
                self.assertTrue(any(analysis_summary in item.value for item in app.markdown))
                self.assertTrue(any(item.value == evidence for item in app.text))
                self.assertEqual(store.news_rows(), before)

    def test_legacy_runtime_log_localizes_display_without_changing_audit_data(self):
        store = self.live_store()
        original = "Fetching quotes / 抓取行情: 10 symbols"
        store.log("INFO", original)
        before = store.logs()
        app = self.set_page(self.app(), "Alerts & Validation")
        english_tables = "\n".join(str(value) for table in app.dataframe for value in table.value.to_numpy().ravel())
        self.assertIn("Fetching quotes", english_tables)
        self.assertIsNone(HAN.search(english_tables))
        self.set_language(app, "中文")
        chinese_tables = "\n".join(str(value) for table in app.dataframe for value in table.value.to_numpy().ravel())
        self.assertTrue(HAN.search(chinese_tables))
        self.assertEqual(store.logs(), before)
        self.assertIsNone(HAN.search(localize_log(original, "en")))
        self.assertTrue(HAN.search(localize_log(original, "zh")))

    def test_news_filter_values_and_results_survive_language_changes(self):
        store = self.live_store()
        stamp = datetime.now(timezone.utc).isoformat()
        for symbol, category, sentiment in (("AAPL", "Finance", "positive"), ("MSFT", "Macro", "negative")):
            item = {"id": f"filter-{symbol}", "headline": f"{symbol} filter fixture", "summary": "Original source text.",
                    "symbols": [symbol], "created_at": stamp, "updated_at": stamp, "received_at": stamp,
                    "data_mode": "live", "source": "Offline fixture", "url": ""}
            version = store.save_news(item)
            store.save_analysis(item["id"], version, {"status": "success", "categories": [category],
                "summary": "Original analysis.", "impacts": [{"symbol": symbol, "sentiment": sentiment,
                "reason": "Original reason.", "evidence": "Original source text."}]})
        app = self.set_page(self.app(), "News Intelligence")
        app.selectbox(key="news_stock_live").set_value("AAPL")
        app.selectbox(key="news_category_live").set_value("Finance")
        app.selectbox(key="news_sentiment_live").set_value("positive").run()
        self.assert_ok(app)
        for language in ("中文", "English"):
            self.set_language(app, language)
            self.assertEqual(app.selectbox(key="news_stock_live").value, "AAPL")
            self.assertEqual(app.selectbox(key="news_category_live").value, "Finance")
            self.assertEqual(app.selectbox(key="news_sentiment_live").value, "positive")
            locale = "zh" if language == "中文" else "en"
            for key, canonical in (("news_stock_live", "AAPL"), ("news_category_live", "Finance"), ("news_sentiment_live", "positive")):
                self.assert_browser_selection_sent(app.selectbox(key=key), canonical, locale)
            visible = "\n".join(item.value for item in app.markdown)
            self.assertIn("AAPL filter fixture", visible)
            self.assertNotIn("MSFT filter fixture", visible)
        self.set_language(app, "中文")
        app.selectbox(key="news_sentiment_live").set_value("negative").run()
        self.assert_ok(app)
        self.assertTrue(any(item.value == translate("No saved articles match these filters.", "zh") for item in app.info))
        self.set_language(app, "English")
        self.assertEqual(app.selectbox(key="news_sentiment_live").value, "negative")
        self.assertTrue(any(item.value == "No saved articles match these filters." for item in app.info))

    def test_language_selection_is_per_session(self):
        first = self.set_language(self.app(), "中文")
        second = self.app()
        self.assertEqual(first.selectbox(key="ui_language").value, "中文")
        self.assertEqual(second.selectbox(key="ui_language").value, "English")
        first.run()
        self.assert_ok(first)
        self.assertEqual(first.selectbox(key="ui_language").value, "中文")


if __name__ == "__main__":
    unittest.main()
