from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_config import ACTION_CONFIG
from cli_runtime import execute_generation
from document_loader import load_document
from orchestrator import main
from result_processor import process_result
from skill_loader import load_skill
from main import (
    discover_unfinished_inputs,
    load_checkpoint,
    resolve_environment_placeholders,
    run_batch,
)


class FakeConnector:
    model = "fake-model"

    def __init__(self, result: str = "generated artifact") -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def generate(self, *, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        return self.result


class ApplicationTests(unittest.TestCase):
    def test_plain_text_loader_preserves_latex(self) -> None:
        latex = r"The result is \(\beta_i\).\n\[\operatorname{Var}(X)=\mathbb{E}[X^2]\]"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "chapter.tex"
            source.write_text(latex, encoding="utf-8")
            self.assertEqual(load_document(source), latex)

    def test_every_action_uses_full_skill_and_source(self) -> None:
        source_text = "Unique source content with \\(x^2\\)."
        skill_text = "UNIQUE COMPLETE SKILL"
        for action, config in ACTION_CONFIG.items():
            with self.subTest(action=action):
                connector = FakeConnector()
                module = importlib.import_module(config["module"])
                result = module.generate(
                    source_text,
                    Path("input/source.tex"),
                    skill_text,
                    connector,
                    Path("tmp/result.txt"),
                )
                self.assertEqual(result, "generated artifact")
                system_prompt, user_prompt = connector.calls[0]
                self.assertIn(skill_text, system_prompt)
                self.assertIn(source_text, user_prompt)
                self.assertIn("source.tex", user_prompt)

    def test_runtime_writes_exact_requested_path(self) -> None:
        connector = FakeConnector("expected output")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.txt"
            output = Path(directory) / "nested" / "named-by-caller.txt"
            source.write_text("Source material", encoding="utf-8")
            with patch("cli_runtime.create_connector", return_value=connector):
                execute_generation(
                    connector_name="ollama",
                    input_path=source,
                    action="create_flashcards",
                    output_path=output,
                )
            self.assertEqual(output.read_text(encoding="utf-8"), "expected output")

    def test_podcast_action_resolves_supplied_legacy_skill_directory(self) -> None:
        loaded = load_skill("podcasts")
        self.assertEqual(loaded.path.parent.name, "podcast-script")
        self.assertIn("# podcast-script", loaded.text)

    def test_json_fence_cleanup_and_validation(self) -> None:
        processed = process_result('```json\n{"ok": true}\n```', Path("out.json"))
        self.assertEqual(json.loads(processed), {"ok": True})

    def test_missing_input_returns_input_error_exit_code(self) -> None:
        exit_code = main(
            [
                "--connector",
                "ollama",
                "--input",
                "definitely-missing.txt",
                "--action",
                "create_reports",
                "--output",
                "unused.txt",
            ]
        )
        self.assertEqual(exit_code, 3)

    def test_environment_placeholders_are_resolved_recursively(self) -> None:
        value = {"provider": {"api_key": "${TEST_TOKEN}", "items": ["x${TEST_TOKEN}"]}}
        resolved = resolve_environment_placeholders(value, {"TEST_TOKEN": "secret"})
        self.assertEqual(resolved["provider"]["api_key"], "secret")
        self.assertEqual(resolved["provider"]["items"], ["xsecret"])

    def test_unfinished_discovery_excludes_checkpoint_basenames(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_directory = root / "input"
            input_directory.mkdir()
            (input_directory / "done.tex").write_text("done", encoding="utf-8")
            (input_directory / "new.tex").write_text("new", encoding="utf-8")
            sources = discover_unfinished_inputs(
                input_directory, {"completed": ["done.tex"]}, root
            )
        self.assertEqual([source.name for source in sources], ["new.tex"])

    def test_batch_marks_source_complete_after_every_action_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_directory = root / "input"
            input_directory.mkdir()
            source = input_directory / "new.tex"
            source.write_text("new", encoding="utf-8")
            checkpoint = root / ".checkpoint.json"
            calls: list[list[str]] = []

            def fake_run(command: list[str], **kwargs: object) -> object:
                del kwargs
                calls.append(command)
                return type("Result", (), {"returncode": 0})()

            config = {"provider": {"default": "ollama"}, "ollama": {"model": "llama3.2"}}
            with patch("main.subprocess.run", side_effect=fake_run), patch(
                "main.run_timestamp", return_value="20260908123456"
            ):
                exit_code = run_batch(
                    config=config,
                    checkpoint_path=checkpoint,
                    input_directory=input_directory,
                    output_directory=root / "tmp",
                    actions=["create_datatables", "create_qandas"],
                )
            saved_checkpoint = json.loads(checkpoint.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("new.tex", saved_checkpoint["completed"])
        self.assertEqual(saved_checkpoint["in_progress"], {})
        self.assertEqual(saved_checkpoint["version"], 2)
        self.assertIn("output_create_datatables_20260908123456.txt", calls[0][-1])

    def test_legacy_checkpoint_is_migrated_to_artifact_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".checkpoint.json"
            path.write_text(
                json.dumps({"completed": ["done.txt"], "in_progress": ["active.txt"]}),
                encoding="utf-8",
            )
            checkpoint = load_checkpoint(path)
        self.assertEqual(checkpoint["version"], 2)
        self.assertEqual(
            checkpoint["in_progress"]["active.txt"],
            {"completed": [], "in_progress": {}},
        )

    def test_batch_resume_skips_completed_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_directory = root / "input"
            input_directory.mkdir()
            (input_directory / "new.tex").write_text("new", encoding="utf-8")
            checkpoint_path = root / ".checkpoint.json"
            results = iter((0, 6))

            def first_run(command: list[str], **kwargs: object) -> object:
                del command, kwargs
                return type("Result", (), {"returncode": next(results)})()

            config = {"provider": {"default": "ollama"}, "ollama": {"model": "llama3.2"}}
            with patch("main.subprocess.run", side_effect=first_run), patch(
                "main.run_timestamp", return_value="20260908123456"
            ):
                self.assertEqual(
                    run_batch(
                        config=config,
                        checkpoint_path=checkpoint_path,
                        input_directory=input_directory,
                        output_directory=root / "tmp",
                        actions=["create_datatables", "create_qandas"],
                    ),
                    6,
                )
            partial = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(partial["in_progress"]["new.tex"]["completed"], ["datatables"])

            calls: list[list[str]] = []

            def resumed_run(command: list[str], **kwargs: object) -> object:
                del kwargs
                calls.append(command)
                return type("Result", (), {"returncode": 0})()

            with patch("main.subprocess.run", side_effect=resumed_run):
                self.assertEqual(
                    run_batch(
                        config=config,
                        checkpoint_path=checkpoint_path,
                        input_directory=input_directory,
                        output_directory=root / "tmp",
                        actions=["create_datatables", "create_qandas"],
                    ),
                    0,
                )
            self.assertEqual(len(calls), 1)
            self.assertIn("create_qandas", calls[0])

    def test_dry_run_does_not_write_checkpoint_or_require_unselected_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_directory = root / "input"
            input_directory.mkdir()
            (input_directory / "new.tex").write_text("new", encoding="utf-8")
            checkpoint = root / ".checkpoint.json"
            config = {
                "provider": {"default": "ollama"},
                "openai": {"api_key": "${MISSING_OPENAI_TOKEN}"},
                "ollama": {"model": "llama3.2"},
            }
            exit_code = run_batch(
                config=config,
                checkpoint_path=checkpoint,
                input_directory=input_directory,
                output_directory=root / "tmp",
                actions=["create_datatables"],
                dry_run=True,
            )
            self.assertFalse(checkpoint.exists())
        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
