from __future__ import annotations

import io
import csv
import json
import os
import tempfile
import unittest
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from cli_runtime import options_from_args, run_action_cli
from connectors.base import GenerationResponse
from errors import ProviderError
from orchestrator import main as orchestrator_main
import study_set


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

    def __init__(self, failure: bool = False, payload: dict | None = None,
                 csv_payload: str | None = None) -> None:
        self.calls: list[dict] = []
        self.failure = failure
        self.payload = payload
        self.csv_payload = csv_payload

    def generate_response(self, **request) -> GenerationResponse:
        self.calls.append(request)
        if self.failure:
            raise ProviderError("Provider failed for this test.")
        text = json.dumps(self.payload if self.payload is not None else podcast())
        if self.csv_payload is not None and ".csv\n" in request["user_prompt"]:
            text = self.csv_payload
        return GenerationResponse(
            text,
            finish_reason="stop", input_tokens=10, output_tokens=20,
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



def config_file(root: Path, *, verbose: bool = False, files: list | None = None,
                pipeline: dict | None = None, model: str | None = "fake-model") -> Path:
    config = {
        "files": files if files is not None else [
            {"input": "input", "output": "output", "inputPattern": "*.txt", "types": ["podcast"]},
        ],
        "verbose": verbose,
    }
    if model is not None:
        config["model"] = model
    if pipeline is not None:
        config["pipeline"] = pipeline
    path = root / "study-set-config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def invoke_prompt(commands: list[str | BaseException], *, prompts: list[str] | None = None) -> tuple[int, str, str]:
    with patch("builtins.input", side_effect=commands) as read:
        result = invoke(study_set.main, [])
    if prompts is not None:
        prompts.extend(call.args[0] for call in read.call_args_list)
    return result


def completed_lines(text: str, *, cursor_position: int | None = None) -> list[str]:
    document = Document(text, cursor_position=cursor_position)
    completions = study_set.StudySetCompleter().get_completions(document, CompleteEvent(completion_requested=True))
    return [document.text_before_cursor[:len(document.text_before_cursor) + completion.start_position]
            + completion.text + document.text_after_cursor for completion in completions]


class StudySetCliTests(unittest.TestCase):
    def test_main_without_arguments_starts_prompt(self) -> None:
        with patch("study_set.run_prompt", return_value=0) as prompt:
            self.assertEqual(study_set.main([]), 0)
        prompt.assert_called_once_with()

    def test_main_uses_prompt_for_process_arguments_and_rejects_old_generation_arguments(self) -> None:
        with patch("sys.argv", ["study_set.py"]), patch("study_set.run_prompt", return_value=0) as prompt:
            self.assertEqual(study_set.main(), 0)
        prompt.assert_called_once_with()
        with patch("study_set.run_prompt") as prompt, redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as failure:
            study_set.main(["book.txt", "podcast"])
        self.assertEqual(failure.exception.code, 2)
        prompt.assert_not_called()

    def test_exit_and_eof_finish_prompt_without_reading_another_command(self) -> None:
        for ending in ("exit", EOFError()):
            with self.subTest(ending=ending), patch("builtins.input", side_effect=[ending]) as read:
                code, _, _ = invoke(study_set.main, [])
            self.assertEqual(code, 0)
            self.assertEqual(read.call_count, 1)

    def test_pwd_is_removed_from_commands_and_help(self) -> None:
        code, stdout, stderr = invoke_prompt(["pwd", "exit"])
        self.assertEqual(code, 0)
        self.assertNotIn("pwd", stdout)
        self.assertIn("Unknown command 'pwd'", stderr)
        self.assertIn("Supported commands: ls, cd, generate, exit", stderr)
        help_text = io.StringIO()
        with redirect_stdout(help_text), self.assertRaises(SystemExit) as finished:
            study_set.main(["--help"])
        self.assertEqual(finished.exception.code, 0)
        self.assertNotIn("pwd", help_text.getvalue())

    def test_completion_matches_commands_case_insensitively_without_pwd(self) -> None:
        self.assertEqual(completed_lines("GE"), ["generate"])
        self.assertEqual(set(completed_lines("")), {"ls", "cd", "generate", "exit"})
        for text in ("pw", "unknown", "generate ", "ls notes"):
            with self.subTest(text=text):
                self.assertEqual(completed_lines(text), [])

    def test_cd_completion_lists_only_directories_and_quotes_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "folder with spaces").mkdir()
            (root / "folder-other").mkdir()
            (root / "folder.txt").write_text("not a directory", encoding="utf-8")
            with chdir(root):
                self.assertEqual(set(completed_lines("cd FOL")), {
                    f'cd "folder with spaces{os.sep}"', f"cd folder-other{os.sep}",
                })
                self.assertEqual(completed_lines("cd missing"), [])
                self.assertEqual(completed_lines("cd 'folder w"), [f"cd 'folder with spaces{os.sep}'"])
                self.assertEqual(completed_lines('cd "folder w"', cursor_position=12),
                                 [f'cd "folder with spaces{os.sep}"'])

    def test_cd_completion_handles_nested_absolute_paths_and_cwd_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            child = root / "nested"
            child.mkdir()
            (child / "chapter").mkdir()
            (root / "other").mkdir()
            completer = study_set.StudySetCompleter()
            event = CompleteEvent(completion_requested=True)
            with chdir(root):
                self.assertEqual(completed_lines("cd nested/ch"), ["cd nested/chapter/"])
                self.assertEqual(completed_lines(f'cd "{child}{os.sep}ch'),
                                 [f'cd "{child}{os.sep}chapter{os.sep}"'])
                first = list(completer.get_completions(Document("cd ch"), event))
                os.chdir(child)
                second = list(completer.get_completions(Document("cd ch"), event))
            self.assertEqual(first, [])
            self.assertEqual([item.text for item in second], [f"chapter{os.sep}"])

    def test_redirected_prompt_uses_input_and_tty_prompt_uses_completion(self) -> None:
        with patch("study_set.sys.stdin.isatty", return_value=False), \
                patch("study_set.PromptSession") as session:
            self.assertIs(study_set._command_reader(), input)
        session.assert_not_called()
        with patch("study_set.sys.stdin.isatty", return_value=True), \
                patch("study_set.sys.stdout.isatty", return_value=True), \
                patch("study_set.PromptSession") as session:
            self.assertEqual(study_set._command_reader(), session.return_value.prompt)
        self.assertIsInstance(session.call_args.kwargs["completer"], study_set.StudySetCompleter)
        self.assertFalse(session.call_args.kwargs["complete_while_typing"])

    def test_prompt_lists_files_and_changes_relative_and_absolute_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            child = root / "folder with spaces"
            child.mkdir()
            (root / "notes.txt").write_text("notes", encoding="utf-8")
            (child / "chapter.txt").write_text("chapter", encoding="utf-8")
            prompts = []
            with chdir(root):
                code, stdout, stderr = invoke_prompt([
                    "ls", 'cd "folder with spaces"', "ls", "cd .",
                    "cd ..", f'cd "{child}"', "exit",
                ], prompts=prompts)
                self.assertEqual(Path.cwd(), child)
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("notes.txt", stdout)
            self.assertIn("chapter.txt", stdout)
            self.assertEqual(prompts, [f"{directory}> " for directory in
                                     (root, root, child, child, child, root, child)])

    def test_prompt_continues_after_unknown_commands_and_invalid_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "file.txt").write_text("not a directory", encoding="utf-8")
            prompts = []
            with chdir(root), patch("study_set.generate_study_sets") as generate:
                code, _, stderr = invoke_prompt([
                    "", "unknown-command", "cd missing", "cd file.txt", "exit",
                ], prompts=prompts)
                self.assertEqual(Path.cwd(), root)
            self.assertEqual(code, 0)
            self.assertEqual(prompts, [f"{root}> "] * 5)
            for name in ("unknown-command", "missing", "file.txt"):
                self.assertIn(name, stderr)
            generate.assert_not_called()

    def test_prompt_generate_uses_current_directory_and_continues_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            child = root / "study"
            child.mkdir()
            prompts = []
            with chdir(root), patch("study_set.generate_study_sets", side_effect=[6, 0]) as generate:
                code, _, _ = invoke_prompt(["generate", "cd study", "generate", "exit"], prompts=prompts)
            self.assertEqual(code, 0)
            self.assertEqual([call.args[0] for call in generate.call_args_list], [root, child])
            self.assertEqual(prompts, [f"{root}> ", f"{root}> ", f"{child}> ", f"{child}> "])

    def test_missing_config_reports_error_and_prompt_remains_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            prompts = []
            with chdir(root), patch("study_set.run_cli_generation") as run:
                code, _, stderr = invoke_prompt(["generate", "exit"], prompts=prompts)
            self.assertEqual(code, 0)
            self.assertIn("study set config was not found", stderr.lower())
            self.assertEqual(prompts, [f"{root}> "] * 2)
            run.assert_not_called()

    def test_generate_resolves_config_paths_and_defaults_to_json_and_the_connector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = source_file(root)
            config_file(root)
            with patch("study_set.run_cli_generation", return_value=0) as run:
                code, stdout, stderr = invoke(study_set.generate_study_sets, root)
            output = root / "output" / source.stem / "podcast" / "podcasts.json"
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn(str(output), stdout)
            run.assert_called_once()
            args = run.call_args.args[0]
            self.assertEqual(args.connector, "the_connector")
            self.assertEqual(args.model, "fake-model")
            self.assertFalse(args.verbose)
            self.assertEqual(run.call_args.kwargs, {
                "action": "create_podcasts", "input_path": source, "output_path": output,
            })

    def test_model_inherits_from_config_and_entry_override_is_trimmed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source_file(root)
            config_file(root, model="  shared-model  ", files=[
                {"input": "input", "output": "output", "inputPattern": "*.txt", "types": ["podcast"]},
                {"input": "input", "output": "output", "inputPattern": "*.txt", "types": ["quiz"],
                 "model": "  entry-model  "},
            ])
            with patch("study_set.run_cli_generation", return_value=0) as run:
                code, _, stderr = invoke(study_set.generate_study_sets, root)
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual([call.args[0].model for call in run.call_args_list], ["shared-model", "entry-model"])

    def test_each_entry_can_supply_a_model_without_a_top_level_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source_file(root)
            config_file(root, model=None, files=[
                {"input": "input", "output": "output", "inputPattern": "*.txt", "types": ["podcast"],
                 "model": "podcast-model"},
                {"input": "input", "output": "output", "inputPattern": "*.txt", "types": ["quiz"],
                 "model": "quiz-model"},
            ])
            with patch("study_set.run_cli_generation", return_value=0) as run:
                code, _, stderr = invoke(study_set.generate_study_sets, root)
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual([call.args[0].model for call in run.call_args_list], ["podcast-model", "quiz-model"])

    def test_missing_model_in_a_later_entry_rejects_entire_batch_before_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source_file(root)
            config_file(root, model=None, files=[
                {"input": "input", "output": "output", "inputPattern": "*.txt", "types": ["podcast"],
                 "model": "podcast-model"},
                {"input": "input", "output": "output", "inputPattern": "*.txt", "types": ["quiz"]},
            ])
            with patch("study_set.run_cli_generation") as run:
                code, _, stderr = invoke(study_set.generate_study_sets, root)
            self.assertEqual(code, 2)
            self.assertIn("model", stderr)
            self.assertFalse((root / "output").exists())
            run.assert_not_called()

    def test_explicit_invalid_models_are_rejected_even_when_another_model_is_available(self) -> None:
        entry = {"input": "input", "output": "output", "inputPattern": "*.txt", "types": ["podcast"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source_file(root)
            path = root / "study-set-config.json"
            for value in (None, "", " \t ", False, 42, [], {}):
                configs = [
                    {"model": value, "files": [{**entry, "model": "entry-model"}]},
                    {"model": "shared-model", "files": [{**entry, "model": value}]},
                ]
                for config in configs:
                    path.write_text(json.dumps(config), encoding="utf-8")
                    with self.subTest(config=config), patch("study_set.run_cli_generation") as run:
                        code, _, stderr = invoke(study_set.generate_study_sets, root)
                    self.assertEqual(code, 2)
                    self.assertIn("model", stderr)
                    run.assert_not_called()

    def test_generate_batches_entries_patterns_types_and_formats_without_duplicate_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            input_directory = root / "input"
            input_directory.mkdir()
            first = input_directory / "chapter.study.txt"
            second = input_directory / "appendix.md"
            third = root / "other" / "extra.txt"
            third.parent.mkdir()
            for source in (first, second, third):
                source.write_text("Study content", encoding="utf-8")
            (input_directory / "ignored.pdf").write_text("not matched", encoding="utf-8")
            (input_directory / "directory.txt").mkdir()
            config_file(root, verbose=True, files=[
                {"input": "input", "output": "results", "inputPattern": ["*.txt", "*.md", "*.txt"],
                 "types": ["datatables"], "formats": ["json", "csv"],
                 "connector": "ollama", "model": "caller-model"},
                {"input": "other", "output": "second-output", "inputPattern": "*.txt",
                 "types": ["quiz", "create_reports"]},
            ])
            with patch("study_set.run_cli_generation", return_value=0) as run:
                code, _, _ = invoke(study_set.generate_study_sets, root)
            self.assertEqual(code, 0)
            expected = {
                (source, action, root / "results" / source.stem / singular / f"{plural}.{extension}")
                for source in (first, second)
                for singular, plural, action in (
                    ("datatable", "datatables", "create_datatables"),
                )
                for extension in ("json", "csv")
            }
            expected.add((third, "create_quizzes", root / "second-output" / "extra" / "quiz" / "quizzes.json"))
            expected.add((third, "create_reports", root / "second-output" / "extra" / "report" / "reports.json"))
            actual = {
                (call.kwargs["input_path"], call.kwargs["action"], call.kwargs["output_path"])
                for call in run.call_args_list
            }
            self.assertEqual(actual, expected)
            self.assertEqual(run.call_count, len(expected))
            for call in run.call_args_list:
                self.assertTrue(call.args[0].verbose)
                if call.kwargs["input_path"] != third:
                    self.assertEqual(call.args[0].connector, "ollama")
                    self.assertEqual(call.args[0].model, "caller-model")
                else:
                    self.assertEqual(call.args[0].model, "fake-model")

    def test_pipeline_settings_merge_defaults_with_entry_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source_file(root)
            config_file(root, pipeline={"strategy": "baseline", "retries": 2, "keep_raw": False}, files=[
                {"input": "input", "output": "output", "inputPattern": "*.txt", "types": ["podcast"],
                 "pipeline": {"retries": 0, "temperature": 0.25}},
            ])
            with patch("study_set.run_cli_generation", return_value=0) as run:
                code, _, _ = invoke(study_set.generate_study_sets, root)
            self.assertEqual(code, 0)
            args = run.call_args.args[0]
            self.assertEqual(args.strategy, "baseline")
            self.assertEqual(args.retries, 0)
            self.assertFalse(args.keep_raw)
            self.assertEqual(args.temperature, 0.25)

    def test_top_level_context_window_is_forwarded_as_a_profile_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source_file(root)
            path = config_file(root)
            config = json.loads(path.read_text(encoding="utf-8"))
            config["context_window"] = 32768
            path.write_text(json.dumps(config), encoding="utf-8")
            with patch("study_set.run_cli_generation", return_value=0) as run:
                code, _, stderr = invoke(study_set.generate_study_sets, root)
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            args = run.call_args.args[0]
            self.assertEqual(args.context_window, 32768)
            self.assertEqual(options_from_args(args).profile_overrides["context_window"], 32768)

    def test_context_window_precedence_prefers_entries_and_same_level_pipeline(self) -> None:
        entry = {"input": "input", "output": "output", "inputPattern": "*.txt", "types": ["podcast"]}
        cases = [
            ({}, {}, None),
            ({"context_window": 32768}, {"context_window": 16384}, 16384),
            ({"pipeline": {"context_window": 32768}}, {"context_window": 16384}, 16384),
            ({"context_window": 32768, "pipeline": {"context_window": 65536}}, {}, 65536),
            ({"context_window": 32768},
             {"context_window": 16384, "pipeline": {"context_window": 8192}}, 8192),
            ({"context_window": 32768}, {"pipeline": {"context_window": 16384}}, 16384),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source_file(root)
            path = root / "study-set-config.json"
            for top_settings, entry_settings, expected in cases:
                config = {"model": "fake-model", **top_settings, "files": [{**entry, **entry_settings}]}
                path.write_text(json.dumps(config), encoding="utf-8")
                with self.subTest(top=top_settings, entry=entry_settings), \
                        patch("study_set.run_cli_generation", return_value=0) as run:
                    code, _, stderr = invoke(study_set.generate_study_sets, root)
                self.assertEqual(code, 0)
                self.assertEqual(stderr, "")
                args = run.call_args.args[0]
                self.assertEqual(args.context_window, expected)
                overrides = options_from_args(args).profile_overrides
                if expected is None:
                    self.assertNotIn("context_window", overrides)
                else:
                    self.assertEqual(overrides["context_window"], expected)

    def test_invalid_direct_context_windows_reject_entire_batch_before_generation(self) -> None:
        entry = {"input": "input", "output": "output", "inputPattern": "*.txt", "types": ["podcast"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source_file(root)
            path = root / "study-set-config.json"
            for value in (None, 0, -1, 32768.0, "32768", True, False):
                configs = [
                    {"model": "fake-model", "context_window": value,
                     "pipeline": {"context_window": 32768}, "files": [{**entry, "context_window": 16384}]},
                    {"model": "fake-model", "context_window": 32768,
                     "files": [entry, {**entry, "types": ["quiz"], "context_window": value,
                                       "pipeline": {"context_window": 16384}}]},
                ]
                for config in configs:
                    path.write_text(json.dumps(config), encoding="utf-8")
                    with self.subTest(config=config), patch("study_set.run_cli_generation") as run:
                        code, _, stderr = invoke(study_set.generate_study_sets, root)
                    self.assertEqual(code, 2)
                    self.assertIn("context_window", stderr)
                    self.assertFalse((root / "output").exists())
                    run.assert_not_called()

    def test_invalid_pipeline_context_windows_remain_rejected_before_generation(self) -> None:
        entry = {"input": "input", "output": "output", "inputPattern": "*.txt", "types": ["podcast"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source_file(root)
            path = root / "study-set-config.json"
            for value in (None, 0, -1, 32768.0, "32768", True, False):
                configs = [
                    {"model": "fake-model", "context_window": 32768,
                     "pipeline": {"context_window": value}, "files": [entry]},
                    {"model": "fake-model", "context_window": 32768,
                     "files": [entry, {**entry, "types": ["quiz"], "context_window": 16384,
                                       "pipeline": {"context_window": value}}]},
                ]
                for config in configs:
                    path.write_text(json.dumps(config), encoding="utf-8")
                    with self.subTest(config=config), patch("study_set.run_cli_generation") as run:
                        code, _, stderr = invoke(study_set.generate_study_sets, root)
                    self.assertEqual(code, 2)
                    self.assertIn("pipeline.context_window", stderr)
                    run.assert_not_called()

    def test_all_artifact_types_accept_singular_plural_and_action_names(self) -> None:
        expected = [
            ("datatable", "datatables"), ("flashcard", "flashcards"),
            ("infographic", "infographics"), ("mindmap", "mindmaps"),
            ("podcast", "podcasts"), ("qanda", "qandas"),
            ("quiz", "quizzes"), ("report", "reports"), ("slide", "slides"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = source_file(root)
            for singular, plural in expected:
                action = "create_" + plural
                for alias in (singular, plural, action):
                    config_file(root, files=[{
                        "input": "input", "output": "output", "inputPattern": "*.txt", "types": [alias],
                    }])
                    with self.subTest(alias=alias), patch("study_set.run_cli_generation", return_value=0) as run:
                        code, _, _ = invoke(study_set.generate_study_sets, root)
                    self.assertEqual(code, 0)
                    self.assertEqual(run.call_args.kwargs["action"], action)
                    self.assertEqual(run.call_args.kwargs["input_path"], source)
                    self.assertEqual(run.call_args.kwargs["output_path"],
                                     root / "output" / source.stem / singular / (plural + ".json"))

    def test_no_matching_files_reports_error_without_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "input").mkdir()
            config_file(root)
            with patch("study_set.run_cli_generation") as run:
                code, _, stderr = invoke(study_set.generate_study_sets, root)
            self.assertNotEqual(code, 0)
            self.assertTrue(stderr)
            run.assert_not_called()

    def test_missing_input_directory_preserves_input_error_code_without_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config_file(root)
            with patch("study_set.run_cli_generation") as run:
                code, _, stderr = invoke(study_set.generate_study_sets, root)
            self.assertEqual(code, 3)
            self.assertIn("Input folder was not found", stderr)
            run.assert_not_called()

    def test_same_stem_output_collisions_are_rejected_before_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = source_file(root)
            source.with_suffix(".md").write_text("Other chapter", encoding="utf-8")
            config_file(root, files=[{
                "input": "input", "output": "output", "inputPattern": ["*.txt", "*.md"], "types": ["podcast"],
            }])
            with patch("study_set.run_cli_generation") as run:
                code, _, stderr = invoke(study_set.generate_study_sets, root)
            self.assertEqual(code, 2)
            self.assertIn("overwrite the same output", stderr)
            self.assertFalse((root / "output").exists())
            run.assert_not_called()

    def test_work_directories_are_unique_for_each_source_type_and_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = source_file(root)
            (source.parent / "chapter 2.txt").write_text("second chapter", encoding="utf-8")
            config_file(root, pipeline={"work_dir": "work"}, files=[
                {"input": "input", "output": "output", "inputPattern": "*.txt",
                 "types": ["datatable"], "formats": ["json", "csv"]},
                {"input": "input", "output": "output", "inputPattern": "*.txt",
                 "types": ["flashcard"], "formats": ["json", "txt"]},
            ])
            with patch("study_set.run_cli_generation", return_value=0) as run:
                code, _, _ = invoke(study_set.generate_study_sets, root)
            self.assertEqual(code, 0)
            self.assertEqual(run.call_count, 8)
            work_directories = [call.args[0].work_dir for call in run.call_args_list]
            self.assertEqual(len(set(work_directories)), run.call_count)
            self.assertTrue(all(path.is_relative_to(root / "work") for path in work_directories))

    def test_unsupported_type_format_combinations_are_rejected_before_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source_file(root)
            for study_type, extension in (("podcast", "csv"), ("quiz", "html"), ("report", "txt")):
                config_file(root, files=[{
                    "input": "input", "output": "output", "inputPattern": "*.txt",
                    "types": [study_type], "formats": [extension],
                }])
                with self.subTest(study_type=study_type, extension=extension), \
                        patch("study_set.run_cli_generation") as run:
                    code, _, stderr = invoke(study_set.generate_study_sets, root)
                self.assertEqual(code, 2)
                self.assertIn("Unsupported format", stderr)
                run.assert_not_called()

    def test_malformed_configs_are_validated_before_any_generation(self) -> None:
        valid_entry = {"input": "input", "output": "output", "inputPattern": "*.txt",
                       "types": ["podcast"], "model": "fake-model"}
        invalid_configs = [
            "{invalid json", [], {}, {"files": "input"}, {"files": []},
            {"files": ["invalid entry"]}, {"files": [{"input": "input"}]},
            {"files": [{**valid_entry, "input": 42}]},
            {"files": [{**valid_entry, "types": "podcast"}]},
            {"files": [{**valid_entry, "types": []}]},
            {"files": [{**valid_entry, "types": ["unknown"]}]},
            {"files": [{**valid_entry, "inputPattern": []}]},
            {"files": [{**valid_entry, "inputPattern": 42}]},
            {"files": [{**valid_entry, "formats": ["unsupported"]}]},
            {"files": [{**valid_entry, "connector": "unsupported"}]},
            {"files": [valid_entry], "verbose": "true"},
            {"files": [valid_entry, {**valid_entry, "types": ["unknown"]}]},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source_file(root)
            path = root / "study-set-config.json"
            for config in invalid_configs:
                path.write_text(config if isinstance(config, str) else json.dumps(config), encoding="utf-8")
                with self.subTest(config=config), patch("study_set.run_cli_generation") as run:
                    code, _, stderr = invoke(study_set.generate_study_sets, root)
                self.assertNotEqual(code, 0)
                self.assertTrue(stderr)
                run.assert_not_called()

    def test_batch_preserves_provider_failure_code_and_processes_later_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = source_file(root)
            (source.parent / "chapter 2.txt").write_text("second source", encoding="utf-8")
            config_file(root)
            with patch("study_set.run_cli_generation", side_effect=[6, 0]) as run:
                code, _, _ = invoke(study_set.generate_study_sets, root)
            self.assertEqual(code, 6)
            self.assertEqual(run.call_count, 2)

    def test_real_pipeline_writes_json_without_verbose_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = source_file(root)
            config_file(root, pipeline={"strategy": "baseline", "retries": 0,
                                        "keep_raw": False, "work_dir": "work"})
            temp_root = root / "existing-temp"
            temp_root.mkdir()
            connector = RecordingConnector()
            with patch.dict(os.environ, {"TEMP": str(temp_root)}), \
                    patch("cli_runtime.create_connector", return_value=connector) as create:
                code, stdout, _ = invoke(study_set.generate_study_sets, root)
            output = root / "output" / source.stem / "podcast" / "podcasts.json"
            self.assertEqual(code, 0)
            self.assertIn(str(output), stdout)
            self.assertNotIn(RESPONSE_CONTENT, stdout)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), podcast())
            create.assert_called_once_with("the_connector", model="fake-model", api_key=None)
            self.assertEqual(len(connector.calls), 1)
            self.assertIn(SOURCE_CONTENT, connector.calls[0]["user_prompt"])
            self.assertEqual(list(temp_root.iterdir()), [])

    def test_verbose_saves_full_exchange_in_existing_temp_folder_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = source_file(root)
            temp_root = root / "existing-temp"
            temp_root.mkdir()
            for index in range(2):
                connector = RecordingConnector()
                config_file(root, verbose=True, pipeline={"strategy": "baseline", "retries": 0,
                                                         "keep_raw": False, "work_dir": f"work-{index}"})
                with patch.dict(os.environ, {"TEMP": str(temp_root)}), \
                        patch("cli_runtime.create_connector", return_value=connector):
                    code, _, _ = invoke(study_set.generate_study_sets, root)
                self.assertEqual(code, 0)
                self.assertFalse(list((root / f"work-{index}").rglob("raw")))

            output = root / "output" / source.stem / "podcast" / "podcasts.json"
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), podcast())
            runs = list((temp_root / "personal-software-knowledge-src-log").iterdir())
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

    def test_real_pipeline_writes_matching_data_table_json_and_csv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = source_file(root)
            fields = [{"name": f"dimension_{index}", "description": "A comparative dimension",
                       "example": "Example"} for index in range(10)]
            row = {field["name"]: "Value with a comma, retained" for field in fields}
            row.update(source_id=source.name, page="N/A")
            payload = {"title": "Study comparison", "fields": fields, "data": [row]}
            csv_text = io.StringIO(newline="")
            csv_writer = csv.DictWriter(csv_text, fieldnames=[field["name"] for field in fields] + ["source_id", "page"])
            csv_writer.writeheader()
            csv_writer.writerow(row)
            connector = RecordingConnector(payload=payload, csv_payload=csv_text.getvalue())
            config_file(root, files=[
                {"input": "input", "output": "output", "inputPattern": "*.txt",
                 "types": ["datatable"], "formats": ["json", "csv"]},
            ], pipeline={"strategy": "baseline", "retries": 0, "keep_raw": False, "work_dir": "work"})
            with patch("cli_runtime.create_connector", return_value=connector):
                code, _, _ = invoke(study_set.generate_study_sets, root)
            self.assertEqual(code, 0)
            output_directory = root / "output" / source.stem / "datatable"
            self.assertEqual(json.loads((output_directory / "datatables.json").read_text(encoding="utf-8")), payload)
            with (output_directory / "datatables.csv").open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                rows = list(reader)
            self.assertEqual(reader.fieldnames, [field["name"] for field in fields] + ["source_id", "page"])
            self.assertEqual(rows, [row])

    def test_verbose_provider_failure_keeps_logs_and_provider_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = source_file(root)
            config_file(root, verbose=True, pipeline={"strategy": "baseline", "retries": 0,
                                                     "keep_raw": False, "work_dir": "work"})
            temp_root = root / "existing-temp"
            temp_root.mkdir()
            connector = RecordingConnector(failure=True)
            with patch.dict(os.environ, {"TEMP": str(temp_root)}), \
                    patch("cli_runtime.create_connector", return_value=connector):
                code, stdout, _ = invoke(study_set.generate_study_sets, root)
            self.assertEqual(code, 6)
            self.assertEqual(stdout, "")
            self.assertFalse((root / "output" / source.stem / "podcast" / "podcasts.json").exists())
            self.assertEqual(len(connector.calls), 1)
            runs = list((temp_root / "personal-software-knowledge-src-log").iterdir())
            self.assertEqual(len(runs), 1)
            self.assertTrue(list((runs[0] / "requests").glob("*.json")))
            events = list((runs[0] / "events").glob("*.json"))
            self.assertTrue(events)
            self.assertIn("pipeline_failure", events[0].read_text(encoding="utf-8"))
            self.assertIn("ProviderError", events[0].read_text(encoding="utf-8"))
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
