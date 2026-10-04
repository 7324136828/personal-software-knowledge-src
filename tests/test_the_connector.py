from __future__ import annotations

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from fastapi.testclient import TestClient

from connectors import create_connector
from connectors.the_connector import TheConnector, discover_models
from errors import ConnectorConfigurationError, ProviderError
from main import connector_environment, configured_provider
import server


class TheConnectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.models = [{"id": "study-route", "object": "model", "context_length": 64000}]
        self.completion = {
            "id": "completion-1", "model": "study-route",
            "choices": [{"message": {"role": "assistant", "content": "Study artifact"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
        }
        self.status = 200
        self.requests = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fixture.requests.append(("GET", self.path, None))
                self.reply({"object": "list", "data": fixture.models})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.requests.append(("POST", self.path, body))
                self.reply(fixture.completion)

            def reply(self, data):
                self.send_response(fixture.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            def log_message(self, *args):
                pass

        self.gateway = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.gateway.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.gateway.server_port}"
        self.environment = patch.dict(os.environ, {"THE_CONNECTOR_BASE_URL": self.base_url, "THE_CONNECTOR_MODEL": "", "THE_CONNECTOR_TIMEOUT": "2"})
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.gateway.shutdown()
        self.gateway.server_close()
        self.thread.join()

    def test_discovery_and_completion_without_provider_credentials(self):
        connector = create_connector("the_connector")
        response = connector.generate_response(
            system_prompt="Complete skill instructions", user_prompt="Source material",
            max_output_tokens=128, json_mode=True,
        )
        self.assertEqual(connector.model, "study-route")
        self.assertEqual(response.text, "Study artifact")
        self.assertEqual((response.input_tokens, response.output_tokens), (20, 8))
        self.assertEqual(response.finish_reason, "stop")
        self.assertEqual(self.requests[0][:2], ("GET", "/v1/models"))
        method, path, body = self.requests[1]
        self.assertEqual((method, path), ("POST", "/v1/chat/completions"))
        self.assertEqual(body["model"], "study-route")
        self.assertEqual(body["messages"], [
            {"role": "system", "content": "Complete skill instructions"},
            {"role": "user", "content": "Source material"},
        ])
        self.assertEqual(body["max_tokens"], 128)
        self.assertFalse(body["stream"])
        self.assertNotIn("temperature", body)
        self.assertNotIn("response_format", body)

    def test_explicit_model_and_configured_model_skip_discovery(self):
        with patch.dict(os.environ, {"THE_CONNECTOR_MODEL": "configured-route"}):
            self.assertEqual(TheConnector().model, "configured-route")
            connector = TheConnector(model="explicit-route", base_url=f"{self.base_url}/v1/")
            self.assertEqual(connector.model, "explicit-route")
            connector.generate_response(system_prompt="Skill", user_prompt="Source", temperature=1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0][1], "/v1/chat/completions")
        self.assertEqual(self.requests[0][2]["temperature"], 1)

    def test_truncation_and_malformed_completions(self):
        connector = TheConnector(model="study-route")
        self.completion["choices"][0]["finish_reason"] = "length"
        self.assertTrue(connector.generate_response(system_prompt="Skill", user_prompt="Source").truncated)
        for invalid in ({"choices": []}, {"choices": [{}]}, {"choices": [{"message": {"content": None}}]}):
            with self.subTest(invalid=invalid):
                self.completion = invalid
                with self.assertRaises(ProviderError):
                    connector.generate(system_prompt="Skill", user_prompt="Source")

    def test_empty_and_invalid_catalogs(self):
        self.models = []
        self.assertEqual(discover_models(), [])
        with self.assertRaisesRegex(ConnectorConfigurationError, "no active models"):
            TheConnector()
        for invalid in ([{"name": "raw-upstream"}], "invalid"):
            self.models = invalid
            with self.assertRaises(ProviderError):
                discover_models()

    def test_http_errors_preserve_status_without_echoing_gateway_body(self):
        self.status = 404
        self.completion = {"error": {"message": "sensitive upstream detail"}}
        with self.assertRaises(ProviderError) as raised:
            TheConnector(model="missing").generate(system_prompt="Skill", user_prompt="Source")
        self.assertEqual(raised.exception.status_code, 404)
        self.assertNotIn("sensitive", str(raised.exception))

    def test_web_catalog_proxy_and_defaults(self):
        client = TestClient(server.app)
        config = client.get("/api/config").json()
        self.assertEqual(config["default_connector"], "the_connector")
        self.assertFalse(config["connectors"]["the_connector"]["requires_key"])
        response = client.get("/api/connectors/the_connector/models")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["models"], self.models)
        self.status = 502
        self.assertEqual(client.get("/api/connectors/the_connector/models").status_code, 502)
        self.assertEqual(client.get("/api/connectors/invalid/models").status_code, 400)

    def test_connection_errors_and_timeout_validation(self):
        from urllib.error import URLError
        with patch("connectors.the_connector.urlopen", side_effect=URLError("offline")):
            with self.assertRaisesRegex(ProviderError, "Start its backend"):
                discover_models()
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ConnectorConfigurationError):
                TheConnector(model="study-route", timeout=timeout)

    def test_batch_configuration_maps_local_gateway_settings(self):
        config = {"provider": {"default": "the_connector"}, "the_connector": {"url": self.base_url, "model": "study-route", "timeout": 600}}
        self.assertEqual(configured_provider(config), "the_connector")
        self.assertEqual(connector_environment(config, "the_connector"), {
            "THE_CONNECTOR_BASE_URL": self.base_url,
            "THE_CONNECTOR_MODEL": "study-route", "THE_CONNECTOR_TIMEOUT": "600",
        })


if __name__ == "__main__":
    unittest.main()
