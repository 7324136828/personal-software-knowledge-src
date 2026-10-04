from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cli_runtime import run_action_cli
from connectors.base import GenerationResponse
from errors import ProviderError
from orchestrator import main as orchestrator_main
from study_set import main


SOURCE_CONTENT = "SOURCE_CONTENT_MARKER: user text includes api_key=USER_CONTENT_TO_RETAIN."
SECRET = "CREDENTIAL_VALUE_MUST_NOT_BE_LOGGED"
RESPONSE_CONTENT = "RESPONSE_CONTENT_MARKER"


def podcast() -> dict:
    return {
        "episode_title": "Study overview",
        "podcast_show": "Study together",
        "cast": [
            {"speaker_id": "maya", "host_id": "HOST_A", "name": "Maya",
             "voice_file": "af_heart", "style": "Calm"},
            {"speaker_id": "leo", "host_id": "HOST_B", "name": "Leo",
             "voice_file": "am_michael", "style": "Curious"},
        ],
        "script": [{"segment_name": "Overview", "scenes": [
            {"speaker_id": "maya", "dialogue": "Welcome. " + RESPONSE_CONTENT},
            {"speaker_id": "leo", "dialogue": "Goodbye."},
        ]}],
    }


class RecordingConnector:
    model = "fake-model"
    api_key = SECRET
    headers = {"Authorization": "Bearer " + SECRET}

    def __init__(self, failure: bool = False) -> None:
        self.calls: list[dict] = []
        self.failure = failure

    def generate_response(self, **request) -> GenerationResponse:
        self.calls.append(request)
        if self.failure:
            raise ProviderError("Provider failed for this test.")
        return GenerationResponse(
            json.dumps(podcast()), finish_reason="stop", input_tokens=10, output_tokens=20,
            raw_metadata={
                "request_id": "safe-request-id",
                "headers": self.headers,
                "api_key": self.api_key,
                "provider": {"authorization": SECRET, "upstream_api_key": SECRET},
            },
        )


def source_file(root: Path) -> Path:
    source = root / "input" / "chapter 1.txt"
    source.parent.mkdir()
    source.write_text(SOURCE_CONTENT, encoding="utf-8")
    return source


def isolated_flags(root: Path) -> list[str]:
    return ["--strategy", "baseline", "--retries", "0", "--no-keep-raw",
            "--work-dir", str(root / "work")]


def invoke(entrypoint, arguments: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = entrypoint(arguments)
    return code, stdout.getvalue(), stderr.getvalue()


class StudySetCliTests(unittest.TestCase):
    def test_positional_defaults_write_json_and_print_only_result_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = source_file(root)
            connector = RecordingConnector()
            with chdir(root), patch.dict(os.environ, {"TEMP": str(root / "os-temp")}), \
                    patch("cli_runtime.create_connector", return_value=connector) as create:
                code, stdout, _ = invoke(main, [str(source), "podcast", *isolated_flags(root)])
            output = root / "output" / source.stem / "podcasts.json"
            self.assertEqual(code, 0)
            self.assertEqual(stdout, str(output.resolve()) + "\n")
            self.assertNotIn(RESPONSE_CONTENT, stdout)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), podcast())
            create.assert_called_once_with("the_connector", model=None, api_key=None)
            self.assertEqual(len(connector.calls), 1)
            self.assertIn(SOURCE_CONTENT, connector.calls[0]["user_prompt"])
            self.assertFalse((root / "os-temp" / "personal-software-knowledge-src-log").exists())

    def test_flag_form_preserves_exact_destination_model_and_pipeline_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = source_file(root)
            output = root / "results" / "caller chosen name.json"
            connector = RecordingConnector()
            with patch("cli_runtime.create_connector", return_value=connector) as create:
                code, stdout, _ = invoke(main, [
                    "-i", str(source), "-t", "podcasts", "-o", str(output),
                    "--connector", "ollama", "--model", "caller-model",
                    "--context-window", "32000", "--profile-max-output-tokens", "4096",
                    "--max-output-tokens", "512", "--temperature", "0.25",
                    *isolated_flags(root),
                ])
            self.assertEqual(code, 0)
            self.assertEqual(stdout, str(output.resolve()) + "\n")
            self.assertEqual(list(output.parent.iterdir()), [output])
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), podcast())
            create.assert_called_once_with("ollama", model="caller-model", api_key=None)
            self.assertEqual(connector.calls[0]["max_output_tokens"], 512)
            self.assertEqual(connector.calls[0]["context_window"], 32000)
            self.assertEqual(connector.calls[0]["temperature"], 0.25)
            run_config = json.loads((root / "work" / "run_config.json").read_text(encoding="utf-8"))
            self.assertEqual(run_config["options"]["retries"], 0)
            self.assertEqual(run_config["options"]["strategy"], "baseline")

    def test_all_artifact_types_accept_singular_plural_and_action_names(self) -> None:
        expected = [
            ("datatable", "datatables"), ("flashcard", "flashcards"),
            ("infographic", "infographics"), ("mindmap", "mindmaps"),
            ("podcast", "podcasts"), ("qanda", "qandas"),
            ("quiz", "quizzes"), ("report", "reports"), ("slide", "slides"),
        ]
        for singular, plural in expected:
            action = "create_" + plural
            for alias in (singular, plural, action):
                with self.subTest(alias=alias), patch("study_set.run_cli_generation", return_value=0) as run:
                    code, _, _ = invoke(main, ["book.txt", alias])
                self.assertEqual(code, 0)
                self.assertEqual(run.call_args.kwargs["action"], action)
                self.assertEqual(run.call_args.kwargs["input_path"], Path("book.txt"))
                self.assertEqual(run.call_args.kwargs["output_path"], Path("output/book") / (plural + ".json"))

    def test_mixed_positional_and_flag_forms(self) -> None:
        for arguments in (
            ["book.txt", "-t", "podcast", "--verbose"],
            ["-i", "book.txt", "podcast", "--verbose"],
        ):
            with self.subTest(arguments=arguments), patch("study_set.run_cli_generation", return_value=0) as run:
                code, _, _ = invoke(main, arguments)
            self.assertEqual(code, 0)
            self.assertEqual(run.call_args.kwargs["action"], "create_podcasts")
            self.assertEqual(run.call_args.kwargs["input_path"], Path("book.txt"))
            self.assertTrue(run.call_args.args[0].verbose)

    def test_invalid_or_conflicting_arguments_exit_before_generation(self) -> None:
        invalid_arguments = [
            [], ["book.txt"], ["book.txt", "unknown"],
            ["book.txt", "podcast", "--input", "other.txt"],
            ["book.txt", "podcast", "--type", "quiz"],
        ]
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments), patch("study_set.run_cli_generation") as run:
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failure:
                    main(arguments)
                self.assertEqual(failure.exception.code, 2)
                run.assert_not_called()

    def test_verbose_saves_full_exchange_in_unique_temp_folders_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = source_file(root)
            temp_root = root / "os-temp"
            for index in range(2):
                output = root / f"result-{index}.json"
                connector = RecordingConnector()
                flags = isolated_flags(root / f"run-{index}")
                with patch.dict(os.environ, {"TEMP": str(temp_root)}), \
                        patch("cli_runtime.create_connector", return_value=connector):
                    code, stdout, _ = invoke(main, [str(source), "podcast", "-o", str(output), "-v", *flags])
                self.assertEqual(code, 0)
                self.assertEqual(stdout, str(output.resolve()) + "\n")
                self.assertFalse((root / f"run-{index}" / "work" / "raw").exists())

            log_root = temp_root / "personal-software-knowledge-src-log"
            runs = list(log_root.iterdir())
            self.assertEqual(len(runs), 2)
            self.assertNotEqual(runs[0], runs[1])
            for run in runs:
                self.assertTrue((run / "run.log").is_file())
                requests = list((run / "requests").glob("*.json"))
                responses = list((run / "responses").glob("*.json"))
                self.assertEqual(len(requests), 1)
                self.assertEqual(len(responses), 1)
                request = json.loads(requests[0].read_text(encoding="utf-8"))["payload"]
                response = json.loads(responses[0].read_text(encoding="utf-8"))["payload"]
                self.assertIn(SOURCE_CONTENT, request["user_prompt"])
                self.assertIn("# podcast-script", request["system_prompt"])
                self.assertEqual(json.loads(response["text"]), podcast())
                self.assertEqual(response["metadata"]["request_id"], "safe-request-id")
                saved_text = "\n".join(path.read_text(encoding="utf-8") for path in run.rglob("*") if path.is_file())
                self.assertIn(RESPONSE_CONTENT, saved_text)
                self.assertNotIn(SECRET, saved_text)
                self.assertNotIn('"headers"', saved_text)

    def test_verbose_provider_failure_keeps_logs_and_provider_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = source_file(root)
            output = root / "result.json"
            connector = RecordingConnector(failure=True)
            with patch.dict(os.environ, {"TEMP": str(root / "os-temp")}), \
                    patch("cli_runtime.create_connector", return_value=connector):
                code, stdout, _ = invoke(main, [str(source), "podcast", "-o", str(output), "-v", *isolated_flags(root)])
            self.assertEqual(code, 6)
            self.assertEqual(stdout, "")
            self.assertFalse(output.exists())
            self.assertEqual(len(connector.calls), 1)
            runs = list((root / "os-temp" / "personal-software-knowledge-src-log").iterdir())
            self.assertEqual(len(runs), 1)
            self.assertTrue(list((runs[0] / "requests").glob("*.json")))
            events = list((runs[0] / "events").glob("*.json"))
            self.assertTrue(events)
            self.assertIn("pipeline_failure", events[0].read_text(encoding="utf-8"))
            self.assertIn("ProviderError", events[0].read_text(encoding="utf-8"))
            self.assertIn("ERROR", (runs[0] / "run.log").read_text(encoding="utf-8"))

    def test_missing_input_preserves_input_exit_code_and_verbose_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"TEMP": str(root / "os-temp")}), \
                    patch("cli_runtime.create_connector") as create:
                code, stdout, _ = invoke(main, [str(root / "missing.txt"), "podcast",
                                              "-o", str(root / "unused.json"), "-v", *isolated_flags(root)])
            self.assertEqual(code, 3)
            self.assertEqual(stdout, "")
            create.assert_not_called()
            runs = list((root / "os-temp" / "personal-software-knowledge-src-log").iterdir())
            self.assertEqual(len(runs), 1)
            self.assertIn("ERROR", (runs[0] / "run.log").read_text(encoding="utf-8"))

    def test_legacy_orchestrator_and_action_cli_accept_verbose(self) -> None:
        for entrypoint, action_arguments in (
            (orchestrator_main, ["--action", "create_podcasts"]),
            (lambda argv: run_action_cli("create_podcasts", argv), []),
        ):
            with self.subTest(entrypoint=entrypoint), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = source_file(root)
                output = root / "result.json"
                connector = RecordingConnector()
                with patch.dict(os.environ, {"TEMP": str(root / "os-temp")}), \
                        patch("cli_runtime.create_connector", return_value=connector):
                    code, _, _ = invoke(entrypoint, [
                        "--connector", "ollama", "--input", str(source), "--output", str(output),
                        "-v", *action_arguments, *isolated_flags(root),
                    ])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(output.read_text(encoding="utf-8")), podcast())
                runs = list((root / "os-temp" / "personal-software-knowledge-src-log").iterdir())
                self.assertEqual(len(runs), 1)
                self.assertTrue(list((runs[0] / "requests").glob("*.json")))
                self.assertTrue(list((runs[0] / "responses").glob("*.json")))


if __name__ == "__main__":
    unittest.main()
