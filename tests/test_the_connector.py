from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from fastapi.testclient import TestClient

from connectors import create_connector
from connectors.the_connector import TheConnector, discover_models
from errors import ConnectorConfigurationError, ProviderError
from main import connector_environment, configured_provider
from pipeline.engine import PipelineOptions, run_pipeline
from skill_loader import load_skill
import server


class TheConnectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.models = [{"id": "study-route", "name": "Study Route", "object": "model", "context_length": 64000}]
        self.detail = {
            "gross_max_input_token": 32000,
            "gross_max_output_token": 4096,
            "context_window": 6,
        }
        self.detail_status = None
        self.configs = []
        self.configs_status = None
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
                if self.path.endswith("/models"):
                    self.reply({"object": "list", "data": fixture.models})
                elif "/configuration/detail/" in self.path:
                    self.reply(fixture.detail, fixture.detail_status)
                elif self.path.endswith("/api/configs"):
                    self.reply(fixture.configs, fixture.configs_status)
                else:
                    self.reply({"error": "Unknown fixture endpoint"}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.requests.append(("POST", self.path, body))
                self.reply(fixture.completion)

            def reply(self, data, status=None):
                self.send_response(fixture.status if status is None else status)
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
        self.assertEqual(self.requests[1][:2], ("GET", "/api/configuration/detail/Study%20Route"))
        method, path, body = self.requests[2]
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
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.requests[0][1], "/v1/models")
        self.assertEqual(self.requests[1][1], "/api/configuration/detail/explicit-route")
        self.assertEqual(self.requests[2][1], "/v1/chat/completions")
        self.assertEqual(self.requests[2][2]["temperature"], 1)

    def test_model_profile_uses_gross_limits_and_caches_selected_configuration(self):
        self.models.insert(0, {"id": "other-route", "name": "Wrong Route", "context_length": 1000})
        connector = TheConnector(model="study-route")
        self.assertEqual(self.requests, [])
        profile = connector.get_model_profile({"context_window": 8192})
        self.assertEqual(profile.context_window, 32000 + 4096)
        self.assertEqual(profile.max_input_tokens, 32000)
        self.assertEqual(profile.max_output_tokens, 4096)
        self.assertEqual(profile.reserved_output_tokens, 4096)
        self.assertEqual(connector.get_model_profile({"context_window": 128000}), profile)
        self.assertEqual([(method, path) for method, path, _ in self.requests], [
            ("GET", "/v1/models"), ("GET", "/api/configuration/detail/Study%20Route"),
        ])

    def test_configuration_name_is_encoded_and_api_v1_uses_gateway_root(self):
        name = "Study / route + 日本語"
        self.models = [{"id": "opaque-library-id", "name": name}]
        for api_path in ("/v1/", "/api/v1/"):
            with self.subTest(api_path=api_path):
                self.requests.clear()
                connector = TheConnector(model="opaque-library-id", base_url=self.base_url + api_path)
                connector.generate_response(system_prompt="Skill", user_prompt="Source")
                self.assertEqual([(method, path) for method, path, _ in self.requests], [
                    ("GET", api_path.rstrip("/") + "/models"),
                    ("GET", "/api/configuration/detail/" + quote(name, safe="")),
                    ("POST", api_path.rstrip("/") + "/chat/completions"),
                ])

    def test_catalog_context_cap_bounds_combined_gross_budget(self):
        self.models[0]["context_length"] = 32768
        profile = TheConnector(model="study-route").get_model_profile()
        self.assertEqual(profile.context_window, 32768)
        self.assertEqual(profile.max_output_tokens, 4096)
        for invalid in (0, -1, True, "32768"):
            with self.subTest(context_length=invalid):
                self.models[0]["context_length"] = invalid
                profile = TheConnector(model="study-route").get_model_profile()
                self.assertEqual(profile.context_window, 32000 + 4096)

    def test_catalog_context_length_is_not_used_without_gross_input_limit(self):
        self.detail = {"context_window": 999999, "gross_max_output_token": 3072}
        connector = TheConnector(model="study-route")
        profile = connector.get_model_profile({"context_window": 16384})
        self.assertEqual(profile.context_window, 16384)
        self.assertEqual(profile.max_output_tokens, 3072)
        self.assertEqual(profile.reserved_output_tokens, 2048)
        self.assertIsNone(profile.max_input_tokens)
        self.assertEqual(connector.get_model_profile().context_window, 8192)

    def test_gross_fields_are_validated_independently_as_positive_integers(self):
        self.models[0].pop("context_length")
        for invalid in (None, 0, -1, True, False, 3.5, "24000", [], {}):
            with self.subTest(invalid_input=invalid):
                self.detail = {"gross_max_input_token": invalid, "gross_max_output_token": 3072}
                profile = TheConnector(model="study-route").get_model_profile({"context_window": 16384})
                self.assertEqual(profile.context_window, 16384)
                self.assertEqual(profile.max_output_tokens, 3072)
                self.assertIsNone(profile.max_input_tokens)
            with self.subTest(invalid_output=invalid):
                self.detail = {"gross_max_input_token": 24000, "gross_max_output_token": invalid}
                profile = TheConnector(model="study-route").get_model_profile({"context_window": 16384})
                self.assertEqual(profile.context_window, 24000 + 2048)
                self.assertEqual(profile.max_input_tokens, 24000)
                self.assertEqual(profile.max_output_tokens, 2048)

    def test_missing_and_malformed_detail_use_configured_or_default_context(self):
        for detail in ({}, {"context_window": 999999}, [], None, "invalid"):
            with self.subTest(detail=detail):
                self.detail = detail
                connector = TheConnector(model="study-route")
                self.assertEqual(connector.get_model_profile({"context_window": 16384}).context_window, 16384)
                default = connector.get_model_profile()
                self.assertEqual(default.context_window, 8192)
                self.assertEqual(default.max_output_tokens, 2048)

    def test_unavailable_detail_preserves_fallback_budget_and_completion(self):
        self.detail_status = 503
        connector = TheConnector(model="study-route")
        self.assertEqual(connector.get_model_profile({"context_window": 16384}).context_window, 16384)
        response = connector.generate_response(
            system_prompt="Skill", user_prompt="A" * 20000, context_window=16384,
        )
        self.assertEqual(response.text, "Study artifact")
        self.assertEqual(self.requests[-1][:2], ("POST", "/v1/chat/completions"))
        self.assertEqual(len([request for request in self.requests if request[0] == "GET"]), 2)

    def test_older_configuration_list_selects_exact_model_instead_of_first_record(self):
        self.configs = [
            {"model_id": "other-route", "config": {"gross_max_input_token": 1000, "gross_max_output_token": 100}},
            {"model_id": "study-route", "config": {"gross_max_input_token": 28000, "gross_max_output_token": 8192}},
        ]
        for status in (404, 409):
            with self.subTest(status=status):
                self.requests.clear()
                self.detail_status = status
                profile = TheConnector(model="study-route").get_model_profile({"context_window": 8192})
                self.assertEqual(profile.context_window, 28000 + 8192)
                self.assertEqual(profile.max_input_tokens, 28000)
                self.assertEqual(profile.max_output_tokens, 8192)
                self.assertEqual(self.requests[-1][:2], ("GET", "/api/configs"))

    def test_older_configuration_list_does_not_choose_missing_or_ambiguous_model(self):
        self.detail_status = 404
        matching = {"model_id": "study-route", "config": {"gross_max_input_token": 28000, "gross_max_output_token": 8192}}
        for configs in ([{**matching, "model_id": "other-route"}], [matching, matching], [], {"data": [matching]}):
            with self.subTest(configs=configs):
                self.configs = configs
                profile = TheConnector(model="study-route").get_model_profile({"context_window": 16384})
                self.assertEqual(profile.context_window, 16384)
                self.assertEqual(profile.max_output_tokens, 2048)

    def test_direct_completion_uses_discovered_input_budget_and_clamps_output(self):
        connector = TheConnector(model="study-route")
        response = connector.generate_response(
            system_prompt="Skill", user_prompt="A" * 20000,
            context_window=8192, max_output_tokens=9000,
        )
        self.assertEqual(response.text, "Study artifact")
        self.assertEqual(self.requests[-1][2]["max_tokens"], 4096)
        connector.generate_response(system_prompt="Skill", user_prompt="Source", max_output_tokens=128)
        self.assertEqual(self.requests[-1][2]["max_tokens"], 128)
        self.assertEqual(len([request for request in self.requests if request[0] == "GET"]), 2)

    def test_pipeline_uses_discovered_budget_to_keep_source_in_one_chunk(self):
        episode = {
            "episode_title": "Study overview", "podcast_show": "Study together",
            "cast": [
                {"speaker_id": "maya", "host_id": "HOST_A", "name": "Maya",
                 "voice_file": "af_heart", "style": "Calm"},
                {"speaker_id": "leo", "host_id": "HOST_B", "name": "Leo",
                 "voice_file": "am_michael", "style": "Curious"},
            ],
            "script": [
                {"segment_name": "Introduction", "scenes": [{"speaker_id": "maya", "dialogue": "Welcome."}]},
                {"segment_name": "Discussion", "scenes": [
                    {"speaker_id": "leo", "dialogue": "Alpha explains its central concept."},
                ]},
                {"segment_name": "Sign-off", "scenes": [{"speaker_id": "maya", "dialogue": "Goodbye."}]},
            ],
        }
        self.completion["choices"][0]["message"]["content"] = json.dumps(episode)
        source_text = ("Alpha explains its central concept. " * 60)[:1775]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_pipeline(
                source_text=source_text, source_path=root / "chapter.txt",
                skill_text=load_skill("podcasts").text,
                connector=TheConnector(model="study-route"), connector_name="the_connector",
                action="create_podcasts", output_path=root / "podcast.json",
                options=PipelineOptions(strategy="chunked", keep_raw=False, retries=0,
                                        work_dir=root / "work", checkpoint=False),
            )
            config = json.loads((root / "work" / "run_config.json").read_text(encoding="utf-8"))
        self.assertEqual(result.metrics["chunk_count"], 1)
        self.assertEqual(result.metrics["provider_calls"], 1)
        self.assertTrue(result.validation["valid"], result.validation)
        self.assertEqual(json.loads(result.text), episode)
        profile = config["model_profile"]
        self.assertEqual(profile["context_window"], 32000 + 4096)
        self.assertEqual(profile["max_input_tokens"], 32000)
        self.assertEqual(profile["max_output_tokens"], 4096)
        posts = [body for method, _, body in self.requests if method == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["max_tokens"], 4096)
        self.assertNotIn("Assemble the source-grounded drafts", posts[0]["messages"][1]["content"])

    def test_gross_input_limit_is_enforced_independently_of_combined_context(self):
        self.detail = {"gross_max_input_token": 6000, "gross_max_output_token": 1000}
        connector = TheConnector(model="study-route")
        with self.assertRaises(ProviderError):
            connector.generate_response(system_prompt="", user_prompt="A" * 12000)
        self.assertFalse(any(method == "POST" for method, _, _ in self.requests))

    def test_configuration_bodies_are_not_recorded_in_exchange_logs(self):
        secret = "UNNEEDED_CONFIGURATION_VALUE_MUST_NOT_BE_LOGGED"
        self.detail["routing"] = {"opaque_credential": secret}
        with patch("connectors.the_connector.record_exchange") as record:
            TheConnector(model="study-route").get_model_profile()
        self.assertNotIn(secret, str(record.call_args_list))
        self.detail_status = 404
        self.configs = [{"model_id": "study-route", "config": {**self.detail, "routing": {"opaque_credential": secret}}}]
        with patch("connectors.the_connector.record_exchange") as record:
            TheConnector(model="study-route").get_model_profile()
        self.assertNotIn(secret, str(record.call_args_list))

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
