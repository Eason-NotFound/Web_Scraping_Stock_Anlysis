import unittest
from unittest.mock import Mock, patch
import signal

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
    @patch("reconnect_web.api_status", return_value={"worker_running": True})
    def test_healthy_connection_does_not_restart_services_or_redeploy(self, status, popen, update, gh):
        with patch("reconnect_web.load_env_file", side_effect=self.settings):
            self.assertEqual(link.connect(), 0)
        popen.assert_not_called()
        update.assert_not_called()
        gh.assert_not_called()
