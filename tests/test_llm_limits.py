"""The max_tokens auto-downgrade path (offline, no real provider)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import TestCase

from mkea_extract.config import LLMConfig
from mkea_extract.llm import OpenAICompatibleLLM, _max_tokens_limit


class LimitParsingTests(TestCase):
    """Read the accepted bound out of whatever wording the provider used."""

    def test_parses_the_qwen_range_message(self):
        body = (
            '{"error":{"message":"<400> InternalError.Algo.InvalidParameter: '
            'Range of max_tokens should be [1, 32768]","type":"invalid_request_error"}}'
        )
        self.assertEqual(_max_tokens_limit(body), 32768)

    def test_parses_other_common_phrasings(self):
        cases = {
            '{"message":"max_tokens must be less than or equal to 8192"}': 8192,
            '{"message":"max_tokens should be at most 4096"}': 4096,
            "Range of max_tokens should be [1, 16384]": 16384,
        }
        for body, expected in cases.items():
            self.assertEqual(_max_tokens_limit(body), expected, body)

    def test_unrelated_errors_yield_no_limit(self):
        for body in ('{"message":"invalid api key"}', "", "rate limited"):
            self.assertIsNone(_max_tokens_limit(body))


class _Handler(BaseHTTPRequestHandler):
    """Rejects max_tokens above ``limit`` exactly like a real provider."""

    limit = 8192
    seen: list[int] = []

    def log_message(self, *args):  # silence the test server
        pass

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append(body.get("max_tokens"))
        if body.get("max_tokens", 0) > self.limit:
            payload = json.dumps(
                {"error": {"message": f"Range of max_tokens should be [1, {self.limit}]"}}
            ).encode()
            self.send_response(400)
        else:
            payload = json.dumps(
                {
                    "choices": [{"message": {"content": '{"ok": true}'}}],
                    "model": "fake",
                    "usage": {},
                }
            ).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class AutoDowngradeTests(TestCase):
    def setUp(self):
        _Handler.seen = []
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def llm(self, **kwargs):
        return OpenAICompatibleLLM(
            LLMConfig(api_key="k", base_url=self.base, model="fake", **kwargs)
        )

    def test_oversized_request_retries_at_the_advertised_limit(self):
        data, _ = self.llm(max_tokens=32768).complete_json([{"role": "user", "content": "hi"}])
        self.assertEqual(data, {"ok": True})
        self.assertEqual(_Handler.seen, [32768, 8192])

    def test_a_request_within_the_limit_is_sent_once(self):
        data, _ = self.llm(max_tokens=4096).complete_json([{"role": "user", "content": "hi"}])
        self.assertEqual(data, {"ok": True})
        self.assertEqual(_Handler.seen, [4096])

    def test_the_downgrade_does_not_consume_the_network_retry_budget(self):
        """Parameter repair is not a failure; flaky-network retries stay available."""
        original = _Handler.do_POST
        state = {"calls": 0}

        def flaky(handler):
            state["calls"] += 1
            body = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))
            _Handler.seen.append(body.get("max_tokens"))
            if body.get("max_tokens", 0) > _Handler.limit:
                payload = json.dumps(
                    {"error": {"message": f"Range of max_tokens should be [1, {_Handler.limit}]"}}
                ).encode()
                handler.send_response(400)
            elif state["calls"] <= 3:          # two 503s after the downgrade
                payload = b'{"error":"busy"}'
                handler.send_response(503)
            else:
                payload = json.dumps(
                    {"choices": [{"message": {"content": '{"ok": true}'}}], "model": "f", "usage": {}}
                ).encode()
                handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)

        _Handler.do_POST = flaky
        self.addCleanup(lambda: setattr(_Handler, "do_POST", original))

        data, _ = self.llm(max_tokens=32768, max_retries=3).complete_json(
            [{"role": "user", "content": "hi"}]
        )
        self.assertEqual(data, {"ok": True})
        self.assertEqual(state["calls"], 4)
