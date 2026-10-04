from __future__ import annotations

import io
import json
import logging
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from connectors.base import GenerationResponse
from connectors.the_connector import TheConnector, _request, discover_models
from diagnostic_logging import record_exchange, verbose_logging
from errors import ProviderError
from pipeline.engine import PipelineOptions, run_pipeline


def events(directory: Path) -> list[dict]:
    return sorted((json.loads(path.read_text(encoding="utf-8")) for path in directory.rglob("*.json")),
                  key=lambda event: event["sequence"])


def http_response(data: dict):
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = 200
    response.read.return_value = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return response


class FakeConnector:
    model = "fake-model"

    def generate_response(self, **request):
        logging.getLogger("content_generator").info("Generating a source-grounded artifact")
        return GenerationResponse(json.dumps({"name": "An idea", "children": []}),
                                  finish_reason="stop", input_tokens=12, output_tokens=9,
                                  raw_metadata={"response_id": "fake-response"})


class DiagnosticLoggingTests(unittest.TestCase):
    def test_disabled_logging_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TEMP": directory}):
            record_exchange("request", {"before": True})
            with verbose_logging(False) as run_dir:
                self.assertIsNone(run_dir)
                record_exchange("request", {"during": True})
            record_exchange("response", {"after": True})
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_pipeline_prompts_response_metadata_and_progress_are_saved(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TEMP": directory}):
            with verbose_logging(True) as run_dir:
                self.assertEqual(run_dir.parent, Path(directory) / "personal-software-knowledge-src-log")
                run_pipeline(
                    source_text="Unicode source: café.", source_path=Path("chapter.txt"),
                    skill_text="Return a canonical mind map.", connector=FakeConnector(),
                    connector_name="the_connector", action="create_mindmaps",
                    output_path=Path(directory) / "map.json",
                    options=PipelineOptions(strategy="baseline", keep_raw=False, work_dir=Path(directory) / "work"),
                )
            saved = events(run_dir)
            request = next(event["payload"] for event in saved if event["kind"] == "pipeline_request")
            response = next(event["payload"] for event in saved if event["kind"] == "pipeline_response")
            self.assertEqual(request["stage"], "baseline")
            self.assertIn("café", request["user_prompt"])
            self.assertIn("canonical mind map", request["system_prompt"])
            self.assertEqual(request["parameters"]["max_output_tokens"], 2048)
            self.assertEqual(response["finish_reason"], "stop")
            self.assertEqual((response["input_tokens"], response["output_tokens"]), (12, 9))
            self.assertEqual(response["metadata"], {"response_id": "fake-response"})
            log = (run_dir / "run.log").read_text(encoding="utf-8")
            self.assertIn("Generating a source-grounded artifact", log)
            self.assertIn("Pipeline started", log)
            self.assertIn("Final validation", log)
            self.assertFalse((Path(directory) / "work" / "raw").exists())

    def test_connector_exact_body_and_complete_response_including_discovery(self) -> None:
        catalog = {"object": "list", "data": [{"id": "study-route", "details": {"active": True}}]}
        completion = {
            "id": "completion-1", "provider_detail": {"cached": True},
            "choices": [{"message": {"role": "assistant", "content": "A useful response."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 8},
        }
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TEMP": directory}), \
                patch("connectors.the_connector.urlopen", side_effect=[http_response(catalog), http_response(completion)]) as urlopen:
            with verbose_logging(True) as run_dir:
                self.assertEqual(discover_models("http://localhost:8301/v1"), catalog["data"])
                TheConnector(model="study-route").generate_response(
                    system_prompt="The full skill", user_prompt="The source", max_output_tokens=128,
                )
            saved = events(run_dir)
            requests = [event["payload"] for event in saved if event["kind"] == "connector_request"]
            responses = [event["payload"] for event in saved if event["kind"] == "connector_response"]
            self.assertEqual((requests[0]["method"], requests[0]["path"], requests[0]["body"]), ("GET", "/v1/models", None))
            self.assertEqual((requests[1]["method"], requests[1]["path"]), ("POST", "/v1/chat/completions"))
            self.assertEqual(requests[1]["body"], json.loads(urlopen.call_args_list[1].args[0].data))
            self.assertEqual(responses[0]["body"], catalog)
            self.assertEqual(responses[1]["body"], completion)
            self.assertNotIn("headers", json.dumps(saved).lower())

    def test_failed_connector_request_and_response_are_saved_without_credentials(self) -> None:
        body = {"error": "Invalid route", "api_key": "do-not-save"}
        failure = HTTPError("http://localhost/v1/chat/completions", 502, "Bad gateway", {},
                            io.BytesIO(json.dumps(body).encode("utf-8")))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TEMP": directory}), \
                patch("connectors.the_connector.urlopen", side_effect=failure):
            with verbose_logging(True) as run_dir, self.assertRaises(ProviderError):
                _request("http://localhost/v1", "chat/completions", 10, {"model": "bad-route"})
            saved = events(run_dir)
            self.assertEqual([event["kind"] for event in saved], ["connector_request", "connector_response", "connector_failure"])
            self.assertEqual(saved[-1]["payload"]["status"], 502)
            self.assertEqual(saved[1]["payload"]["body"], {"error": "Invalid route"})
            self.assertNotIn("do-not-save", json.dumps(saved))

    def test_connector_destination_omits_url_credentials_and_query(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TEMP": directory}), \
                patch("connectors.the_connector.urlopen", return_value=http_response({"ok": True})):
            with verbose_logging(True) as run_dir:
                _request("http://user:do-not-save@localhost:8301/v1", "models?api_key=do-not-save", 10)
            saved = events(run_dir)
            self.assertEqual(saved[0]["payload"]["origin"], "http://localhost:8301")
            self.assertEqual(saved[0]["payload"]["path"], "/v1/models")
            self.assertNotIn("do-not-save", json.dumps(saved))

    def test_pipeline_failed_attempt_is_logged_even_without_raw_evidence(self) -> None:
        connector = FakeConnector()
        connector.generate_response = MagicMock(side_effect=ProviderError("Route rejected the request."))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TEMP": directory}):
            with verbose_logging(True) as run_dir, self.assertRaises(ProviderError):
                run_pipeline(
                    source_text="A source.", source_path=Path("chapter.txt"), skill_text="Make a mind map.",
                    connector=connector, connector_name="the_connector", action="create_mindmaps",
                    output_path=Path(directory) / "map.json",
                    options=PipelineOptions(strategy="baseline", keep_raw=False, retries=0, work_dir=Path(directory) / "work"),
                )
            saved = events(run_dir)
            self.assertEqual([event["kind"] for event in saved], ["pipeline_request", "pipeline_failure"])
            self.assertEqual(saved[-1]["payload"]["stage"], "baseline")
            self.assertEqual(saved[-1]["payload"]["error_type"], "ProviderError")

    def test_repeated_contexts_restore_handlers_levels_and_do_not_capture_other_threads(self) -> None:
        logger = logging.getLogger("content_generator")
        original_handlers, original_level = list(logger.handlers), logger.level
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TEMP": directory}):
            runs = []
            for index in range(2):
                with verbose_logging(True) as run_dir:
                    runs.append(run_dir)
                    logger.debug("Own run %s", index)
                    thread = threading.Thread(target=lambda: logger.info("Unrelated web thread"))
                    thread.start()
                    thread.join()
                    record_exchange("event", {"headers": {"Authorization": "do-not-save"}, "api_key": "do-not-save", "ok": True})
                self.assertEqual(logger.handlers, original_handlers)
                self.assertEqual(logger.level, original_level)
                self.assertNotIn("Unrelated web thread", (run_dir / "run.log").read_text(encoding="utf-8"))
                self.assertEqual(events(run_dir)[0]["payload"], {"ok": True})
            self.assertNotEqual(runs[0], runs[1])
            record_exchange("after", {"not_saved": True})
            self.assertEqual(len(events(runs[-1])), 1)


if __name__ == "__main__":
    unittest.main()
