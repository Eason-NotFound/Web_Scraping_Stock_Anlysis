"""Offline launcher, visible monitor, and shutdown regressions.

All credentials are synthetic and all SQLite files live in project-local
TemporaryDirectories. These tests never load the project's real .env.
"""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import os
from pathlib import Path
import signal
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from streamlit.testing.v1 import AppTest

import run_local
from newslens import worker
from newslens.storage import Store


ROOT = Path(__file__).resolve().parents[1]
FAKE_ENV = {
    "ALPACA_API_KEY": "test-alpaca-key-only",
    "ALPACA_SECRET_KEY": "test-alpaca-secret-only",
    "LLM_API_KEY": "test-llm-secret-only",
    "LLM_BASE_URL": "https://example.invalid/api/v1",
    "LLM_MODEL": "offline-test-model",
    "NEWSLENS_ENABLE_TELEGRAM": "0",
    "NEWSLENS_ADMIN_TOKEN": "",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
}


class MonitorConsoleTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "work" / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = TemporaryDirectory(prefix="monitor-test-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        previous_cwd = Path.cwd()
        self.addCleanup(os.chdir, previous_cwd)
        self.env_patch = patch.dict(os.environ, FAKE_ENV, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.network_patch = patch("newslens.data_provider.urlopen", side_effect=AssertionError("Network is forbidden in this test"))
        self.network = self.network_patch.start()
        self.addCleanup(self.network_patch.stop)
        self.settings = SimpleNamespace(
            mode="live", db_path=str(self.root / "live.sqlite"), symbols=["AAPL"],
            feed="iex", quote_interval=60, news_interval=180, max_age_seconds=180,
        )

    def store(self):
        store = Store(self.settings.db_path)
        store.init()
        store.assert_mode("live")
        return store

    def provider(self):
        provider = Mock()
        provider.daily_bars.return_value = []
        provider.snapshots.return_value = []
        provider.news.return_value = []
        return provider

    def test_dotenv_preserves_quotes_and_shell_text_without_execution(self):
        sentinel = self.root / "must-not-exist"
        path = self.root / ".env"
        command = f"$(touch {sentinel})"
        backticks = f"`touch {sentinel}`"
        path.write_text(
            "# test-only values\n"
            "PLAIN=plain-value\n"
            'QUOTED="spaces # stay literal"\n'
            "SINGLE='spaces and $HOME'\n"
            f'COMMAND="{command}"\n'
            f"BACKTICKS='{backticks}'\n",
            encoding="utf-8",
        )
        values = run_local.load_env_file(path)
        self.assertEqual(values["PLAIN"], "plain-value")
        self.assertEqual(values["QUOTED"], "spaces # stay literal")
        self.assertEqual(values["SINGLE"], "spaces and $HOME")
        self.assertEqual(values["COMMAND"], command)
        self.assertEqual(values["BACKTICKS"], backticks)
        self.assertFalse(sentinel.exists())
        self.assertNotIn("PLAIN", os.environ, "Parsing must not mutate the process environment")

    def test_malformed_dotenv_error_excludes_secret_value(self):
        path = self.root / ".env"
        secret = "synthetic-secret-do-not-echo"
        path.write_text(f"GOOD=value\nINVALID KEY={secret}\n", encoding="utf-8")
        with self.assertRaises(ValueError) as error:
            run_local.load_env_file(path)
        self.assertIn("line 2", str(error.exception))
        self.assertNotIn(secret, str(error.exception))

    def test_launcher_check_forces_live_without_starting_worker_or_network(self):
        path = self.root / ".env"
        path.write_text("NEWSLENS_MODE=demo\nNEWSLENS_DB=/not-the-selected-db.sqlite\n", encoding="utf-8")
        output = io.StringIO()
        with patch.object(run_local, "ROOT", self.root), patch.object(worker, "main") as start_worker, \
                patch("subprocess.run") as run_process, patch("subprocess.Popen") as spawn_process, redirect_stdout(output):
            result = run_local.main(["worker", "--check"])
        self.assertEqual(result, 0)
        self.assertEqual(os.environ["NEWSLENS_MODE"], "live")
        self.assertEqual(os.environ["NEWSLENS_DB"], str(self.root / "data" / "live.sqlite"))
        start_worker.assert_not_called()
        run_process.assert_not_called()
        spawn_process.assert_not_called()
        self.network.assert_not_called()
        self.assertFalse((self.root / "data" / "live.sqlite").exists())
        for key in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "LLM_API_KEY"):
            self.assertNotIn(FAKE_ENV[key], output.getvalue())

    def test_console_store_redacts_credentials_in_database_and_stdout(self):
        class FlushedBuffer(io.StringIO):
            flush_count = 0
            def flush(self):
                self.flush_count += 1
                super().flush()

        store = worker.ConsoleStore(self.root / "console.sqlite")
        store.init()
        output = FlushedBuffer()
        text = "Visible progress " + " ".join(FAKE_ENV[k] for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "LLM_API_KEY"))
        with redirect_stdout(output):
            store.log("INFO", text)
        persisted = store.logs()[0]["message"]
        self.assertIn("Visible progress", persisted)
        self.assertIn("Visible progress", output.getvalue())
        self.assertGreater(output.flush_count, 0)
        for key in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "LLM_API_KEY"):
            self.assertNotIn(FAKE_ENV[key], persisted)
            self.assertNotIn(FAKE_ENV[key], output.getvalue())

    def test_once_releases_lease_and_restores_signal_handlers(self):
        originals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        with patch.object(worker, "load_settings", return_value=self.settings), \
                patch.object(worker, "AlpacaProvider", return_value=self.provider()), \
                patch.object(worker, "Analyzer") as analyzer, redirect_stdout(io.StringIO()):
            result = worker.main(["--once", "--console"])
        self.assertEqual(result, 0)
        store = self.store()
        self.assertIsNone(store.get_state("worker_lease"))
        self.assertIsNotNone(store.get_state("worker_heartbeat"))
        analyzer.return_value.analyze.assert_not_called()
        self.assertEqual({sig: signal.getsignal(sig) for sig in originals}, originals)
        self.network.assert_not_called()

    def test_duplicate_worker_preserves_existing_owner_and_avoids_provider(self):
        store = self.store()
        self.assertTrue(store.acquire_lease("existing-worker", time.time(), ttl=1800))
        existing = store.get_state("worker_lease")
        with patch.object(worker, "load_settings", return_value=self.settings), \
                patch.object(worker, "AlpacaProvider") as provider, redirect_stdout(io.StringIO()):
            result = worker.main(["--once", "--console"])
        self.assertEqual(result, 2)
        provider.assert_not_called()
        self.assertEqual(store.get_state("worker_lease"), existing)
        self.network.assert_not_called()

    def test_keyboard_interrupt_releases_lease_without_running_next_phase(self):
        provider = self.provider()
        provider.daily_bars.side_effect = KeyboardInterrupt
        originals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        with patch.object(worker, "load_settings", return_value=self.settings), \
                patch.object(worker, "AlpacaProvider", return_value=provider), \
                patch.object(worker, "Analyzer"), redirect_stdout(io.StringIO()):
            result = worker.main(["--console"])
        self.assertEqual(result, 0)
        self.assertIsNone(self.store().get_state("worker_lease"))
        provider.snapshots.assert_not_called()
        provider.news.assert_not_called()
        self.assertEqual({sig: signal.getsignal(sig) for sig in originals}, originals)

    def test_provider_setup_failure_releases_lease_and_does_not_echo_secret(self):
        output = io.StringIO()
        with patch.object(worker, "load_settings", return_value=self.settings), \
                patch.object(worker, "AlpacaProvider", side_effect=RuntimeError(FAKE_ENV["ALPACA_SECRET_KEY"])), \
                redirect_stdout(output):
            result = worker.main(["--once", "--console"])
        self.assertEqual(result, 2)
        self.assertIsNone(self.store().get_state("worker_lease"))
        self.assertNotIn(FAKE_ENV["ALPACA_SECRET_KEY"], output.getvalue())
        self.network.assert_not_called()

    def test_live_ui_without_worker_reports_stopped(self):
        os.environ.update(NEWSLENS_MODE="live", NEWSLENS_DB=self.settings.db_path)
        self.store()
        with patch("streamlit.secrets", {}):
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any("Background monitoring is stopped" in item.value for item in app.warning))
        self.assertIsNone(self.store().get_state("worker_lease"))
        self.network.assert_not_called()


    def test_live_ui_fresh_worker_activity_reports_running(self):
        os.environ.update(NEWSLENS_MODE="live", NEWSLENS_DB=self.settings.db_path)
        store = self.store()
        store.acquire_lease("fixture-worker", time.time(), ttl=1800)
        store.put_state("worker_last_seen", datetime.now(timezone.utc).isoformat())
        with patch("streamlit.secrets", {}):
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any("Background monitoring is running" in item.value for item in app.success))
        self.assertFalse(any("Background monitoring is stopped" in item.value for item in app.warning))
        self.assertEqual(store.get_state("worker_lease")["owner"], "fixture-worker")
        self.network.assert_not_called()

    def test_live_ui_stale_activity_does_not_claim_running(self):
        os.environ.update(NEWSLENS_MODE="live", NEWSLENS_DB=self.settings.db_path)
        store = self.store()
        store.acquire_lease("fixture-worker", time.time(), ttl=1800)
        store.put_state("worker_last_seen", (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat())
        with patch("streamlit.secrets", {}):
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any("The worker lease is still valid, but no recent process heartbeat" in item.value for item in app.warning))
        self.assertFalse(any("Background monitoring is running" in item.value for item in app.success))
        self.assertEqual(store.get_state("worker_lease")["owner"], "fixture-worker")
        self.network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
