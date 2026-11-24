from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import signal
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import reconnect_web as link


class ConnectionRecoveryTests(unittest.TestCase):
    @patch("reconnect_web.requests.get")
    def test_private_token_is_not_sent_to_arbitrary_origin(self, get):
        for origin in ("https://example.com", "https://safe.trycloudflare.com.evil.test", "https://safe.trycloudflare.com/path", "http://safe.trycloudflare.com"):
            with self.assertRaises(ValueError):
                link.api_status(origin, "private-test-token")
        get.assert_not_called()

    @patch("reconnect_web.requests.get")
    def test_protected_request_does_not_follow_redirect(self, get):
        get.return_value = Mock(status_code=302)
        self.assertIsNone(link.api_status("https://safe.trycloudflare.com", "private-test-token"))
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    @patch("reconnect_web.requests.get")
    def test_recognizes_live_api_and_rejects_demo_or_invalid_body(self, get):
        response = Mock(status_code=200)
        get.return_value = response
        response.json.return_value = {"meta": {"data_mode": "live"}, "data": {"worker_running": True}}
        self.assertEqual(link.api_status(link.PUBLIC), {"worker_running": True})
        for body in ({"meta": {"data_mode": "demo"}, "data": {}}, {"meta": {"data_mode": "live"}, "data": []}, []):
            response.json.return_value = body
            self.assertIsNone(link.api_status(link.PUBLIC))

    def test_tunnel_readiness_can_succeed_after_the_old_startup_window(self):
        process = Mock(poll=Mock(return_value=None))
        log = Mock(read_text=Mock(return_value="Visit https://safe.trycloudflare.com"))
        ready = {"data_available": True}
        with patch.object(link.time, "monotonic", side_effect=[0, 46, 53]), \
             patch.object(link.time, "sleep"), \
             patch.object(link, "api_status", side_effect=[None, ready]) as status:
            self.assertEqual(link.wait_for_tunnel(process, log, "test-token"), "https://safe.trycloudflare.com")
        self.assertEqual(status.call_count, 2)

    @patch("reconnect_web.os.kill")
    @patch("reconnect_web.subprocess.run")
    def test_reused_pid_for_unrelated_process_is_not_stopped(self, run, kill):
        run.return_value = Mock(returncode=0, stdout="python -m newslens.worker\n")
        link.stop_own_tunnel(1234)
        kill.assert_not_called()

    @patch("reconnect_web.os.kill")
    @patch("reconnect_web.subprocess.run")
    def test_only_verified_owned_tunnel_receives_stop_signal(self, run, kill):
        run.return_value = Mock(returncode=0, stdout=str(link.ROOT / "work/tools/cloudflared") + " tunnel --url " + link.LOCAL + " --protocol http2\n")
        link.stop_own_tunnel(1234)
        kill.assert_called_once_with(1234, signal.SIGINT)

    def settings(self, _):
        return {"NEWSLENS_API_TOKEN": "private-test-token", "CLOUDFLARE_ACCOUNT_ID": "a" * 32, "CLOUDFLARE_PROJECT_NAME": "newslens-gp", "CLOUDFLARE_API_TOKEN": "deploy-test-token"}

    @patch("reconnect_web.gh")
    @patch("reconnect_web.requests.patch")
    @patch("reconnect_web.subprocess.Popen")
    @patch("reconnect_web.api_status", return_value=None)
    def test_check_mode_does_not_change_services_or_cloud_configuration(self, status, popen, update, gh):
        with patch("reconnect_web.load_env_file", side_effect=self.settings):
            self.assertEqual(link.connect(check=True), 1)
        popen.assert_not_called()
        update.assert_not_called()
        gh.assert_not_called()

    @patch("reconnect_web.gh")
    @patch("reconnect_web.requests.patch")
    @patch("reconnect_web.subprocess.Popen")
    @patch("reconnect_web.api_status", return_value={"data_available": True, "worker_running": True})
    def test_healthy_connection_does_not_restart_services_or_redeploy(self, status, popen, update, gh):
        with patch("reconnect_web.load_env_file", side_effect=self.settings):
            self.assertEqual(link.connect(), 0)
        popen.assert_not_called()
        update.assert_not_called()
        gh.assert_not_called()

    def test_check_reports_database_and_monitor_separately_without_a_runtime_directory(self):
        ready = {"data_available": True, "worker_running": False}
        unreadable = {"data_available": False, "worker_running": False}
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            for local, public, expected, monitor in (
                (ready, ready, 0, "paused"), (unreadable, unreadable, 1, "unavailable"),
                (ready, unreadable, 1, "paused"), (None, ready, 1, "unavailable"),
            ):
                output = StringIO()
                with self.subTest(local=local, public=public), redirect_stdout(output), \
                     patch("sys.argv", ["reconnect_web.py", "--check"]), \
                     patch.object(link, "RUNTIME", runtime), \
                     patch.object(link, "load_env_file", side_effect=self.settings), \
                     patch.object(link, "api_status", side_effect=[local, public]):
                    self.assertEqual(link.main(), expected)
                    self.assertIn("Monitor: " + monitor, output.getvalue())
                    if public is unreadable:
                        self.assertIn("Public website: connected (saved data unavailable)", output.getvalue())
                    self.assertFalse(runtime.exists())

    @patch("reconnect_web.gh")
    @patch("reconnect_web.requests.patch")
    @patch("reconnect_web.subprocess.Popen")
    def test_database_failure_does_not_restart_or_redeploy(self, popen, update, gh):
        ready = {"data_available": True, "worker_running": False}
        unreadable = {"data_available": False, "worker_running": False}
        for local, public, service in ((unreadable, None, "Local API"), (ready, unreadable, "Public website")):
            with self.subTest(service=service), patch.object(link, "load_env_file", side_effect=self.settings), \
                 patch.object(link, "api_status", side_effect=[local, public]):
                with self.assertRaisesRegex(RuntimeError, service + " is reachable, but saved data is unavailable"):
                    link.connect()
        popen.assert_not_called()
        update.assert_not_called()
        gh.assert_not_called()

    def test_post_deployment_reachable_with_unreadable_database_is_a_failure(self):
        ready = {"data_available": True, "worker_running": False}
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            connection = runtime / "connection.json"
            connection.write_text('{"api_base":"https://safe.trycloudflare.com"}')
            with patch.object(link, "RUNTIME", runtime), patch.object(link, "load_env_file", side_effect=self.settings), \
                 patch.object(link, "api_status", side_effect=[ready, None, ready, {"data_available": False}]), \
                 patch.object(link.requests, "patch", return_value=Mock(status_code=200, json=lambda: {"success": True})), \
                 patch.object(link.time, "sleep"), \
                 patch.object(link, "gh", side_effect=["https://github.com/test/project/actions/runs/1",
                                                       RuntimeError("temporary status failure"),
                                                       '{"status":"completed","conclusion":"success"}']) as gh:
                with self.assertRaisesRegex(RuntimeError, "Public website is reachable, but saved data is unavailable"):
                    link.connect()
                self.assertEqual(gh.call_count, 3)
                self.assertEqual(gh.call_args_list[1], gh.call_args_list[2])
            self.assertIn('"deployment_pending": true', connection.read_text())
