import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
import urllib.error

# Prepare environment before importing board
d = tempfile.mkdtemp()
os.environ["BOARD_DB"] = os.path.join(d, "test.db")
os.environ["BOARD_TOKEN"] = "test-token"
os.environ["BOARD_HUMAN"] = "human"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import board


class DummyServer(HTTPServer):
    def __init__(self, handler_class):
        super().__init__(("127.0.0.1", 0), handler_class)
        self.request_count = 0
        self.response_code = 200

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_port}/board"


class DummyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.request_count += 1
        self.send_response(self.server.response_code)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class TestNtfyRetryAndCounter(unittest.TestCase):
    def setUp(self):
        self.server = DummyServer(DummyHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        board.NTFY_URL = self.server.url
        board.ntfy_failures_since_success = 0

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_transient_5xx_retries_and_increments_counter(self):
        self.server.response_code = 503
        t = board.ntfy("test title", "test message")
        if t:
            t.join(timeout=5)
        else:
            # If ntfy doesn't return thread, wait a bit
            import time
            time.sleep(1)

        # Transient 5xx should retry: 3 attempts total (initial + 2 retries)
        self.assertEqual(self.server.request_count, 3)
        self.assertEqual(board.ntfy_failures_since_success, 1)

    def test_transient_network_error_retries(self):
        # Mock urlopen to raise URLError / timeout
        board.ntfy_failures_since_success = 0
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("DNS lookup failed")) as mock_urlopen:
            t = board.ntfy("test title", "test message")
            if t:
                t.join(timeout=5)
            else:
                import time
                time.sleep(1)

            self.assertEqual(mock_urlopen.call_count, 3)
            self.assertEqual(board.ntfy_failures_since_success, 1)

    def test_client_error_403_does_not_retry(self):
        self.server.response_code = 403
        t = board.ntfy("test title", "test message")
        if t:
            t.join(timeout=5)
        else:
            import time
            time.sleep(1)

        # 4xx client errors should NOT be retried: exactly 1 attempt
        self.assertEqual(self.server.request_count, 1)
        self.assertEqual(board.ntfy_failures_since_success, 1)

    def test_counter_resets_on_success(self):
        board.ntfy_failures_since_success = 4
        self.server.response_code = 200
        t = board.ntfy("test title", "test message")
        if t:
            t.join(timeout=5)
        else:
            import time
            time.sleep(1)

        self.assertEqual(self.server.request_count, 1)
        self.assertEqual(board.ntfy_failures_since_success, 0)

    def test_transient_error_then_success_resets_counter(self):
        # First request fails with 500, second succeeds with 200
        board.ntfy_failures_since_success = 2
        calls = 0

        def handler_side_effect():
            nonlocal calls
            calls += 1
            if calls == 1:
                self.server.response_code = 500
            else:
                self.server.response_code = 200

        original_do_POST = DummyHandler.do_POST

        def custom_do_POST(handler_self):
            handler_side_effect()
            original_do_POST(handler_self)

        with patch.object(DummyHandler, "do_POST", custom_do_POST):
            t = board.ntfy("test title", "test message")
            if t:
                t.join(timeout=5)
            else:
                import time
                time.sleep(1)

        self.assertEqual(self.server.request_count, 2)
        self.assertEqual(board.ntfy_failures_since_success, 0)

    def test_status_api_exposes_counter(self):
        board.ntfy_failures_since_success = 7
        st = board.status()
        self.assertEqual(st.get("ntfy_failures_since_success"), 7)

        board.ntfy_failures_since_success = 0
        st = board.status()
        self.assertEqual(st.get("ntfy_failures_since_success"), 0)

    def test_status_html_shows_warning_when_counter_positive(self):
        board.ntfy_failures_since_success = 3
        html = board.html_status(None)
        self.assertIn("ntfy push failed", html)
        self.assertIn("3", html)

    def test_status_html_no_warning_when_counter_zero(self):
        board.ntfy_failures_since_success = 0
        html = board.html_status(None)
        self.assertNotIn("ntfy push failed", html)


if __name__ == "__main__":
    unittest.main()
