from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import traceback
import unittest
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import runtime_environment
import run
import setup
from cli_runtime import run_action_cli
from orchestrator import main as orchestrator_main
from pipeline.engine import PipelineResult
from main import main as batch_main
from experiment import main as experiment_main
import study_set


ROOT = Path(__file__).resolve().parents[1]
SECRET = "test-credential-must-stay-private"


class RuntimeEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.env_file = self.root / ".env"
        self.enterContext(patch("runtime_environment.ENV_FILE", self.env_file))
        self.enterContext(patch.dict(os.environ, {}, clear=True))

    def write_env(self, text: str) -> None:
        self.env_file.write_text(text, encoding="utf-8")

    def test_repository_env_path_is_anchored_to_module(self) -> None:
        # Test the real module constant in a fresh interpreter (not our fixture).
        result = subprocess.run(
            [sys.executable, "-c",
             "import runtime_environment; from pathlib import Path; "
             "assert runtime_environment.ENV_FILE == "
             "Path(runtime_environment.__file__).resolve().parent / '.env'"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_loads_only_project_env_from_another_working_directory(self) -> None:
        self.write_env(f"OPENAI_API_KEY={SECRET}\nOPENAI_MODEL=project-model\n")
        other = self.root / "elsewhere"
        other.mkdir()
        (other / ".env").write_text("OPENAI_MODEL=untrusted-cwd\n", encoding="utf-8")
        with chdir(other):
            runtime_environment.load_environment()
        self.assertEqual(os.environ["OPENAI_MODEL"], "project-model")
        self.assertEqual(os.environ["OPENAI_API_KEY"], SECRET)

    def test_missing_env_does_not_search_cwd_or_parents(self) -> None:
        project = self.root / "project"
        project.mkdir()
        (self.root / ".env").write_text("OPENAI_API_KEY=foreign\n", encoding="utf-8")
        caller = self.root / "caller"
        caller.mkdir()
        (caller / ".env").write_text("OPENAI_MODEL=foreign\n", encoding="utf-8")
        os.environ["HOST"] = "127.0.0.1"
        before = dict(os.environ)
        with patch("runtime_environment.ENV_FILE", project / ".env"), chdir(caller):
            runtime_environment.load_environment()
        self.assertEqual(dict(os.environ), before)

    def test_existing_values_including_empty_values_win_on_repeated_loads(self) -> None:
        self.write_env("OPENAI_API_KEY=file-key\nOPENAI_MODEL=file-model\nPORT=9000\n")
        os.environ.update(OPENAI_API_KEY="shell-key", OPENAI_MODEL="", PORT="8100")
        runtime_environment.load_environment()
        runtime_environment.load_environment()
        self.assertEqual(os.environ["OPENAI_API_KEY"], "shell-key")
        self.assertEqual(os.environ["OPENAI_MODEL"], "")
        self.assertEqual(os.environ["PORT"], "8100")

    def test_quotes_utf8_empty_and_literal_values_without_credential_output(self) -> None:
        self.write_env(
            f"OPENAI_API_KEY='{SECRET} # quoted'\n"
            "OPENAI_MODEL=\"模型 with spaces\"\n"
            "EMPTY=\nLITERAL=${OPENAI_API_KEY}\n"
        )
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            runtime_environment.load_environment()
        self.assertEqual(os.environ["OPENAI_API_KEY"], SECRET + " # quoted")
        self.assertEqual(os.environ["OPENAI_MODEL"], "模型 with spaces")
        self.assertEqual(os.environ["EMPTY"], "")
        self.assertEqual(os.environ["LITERAL"], "${OPENAI_API_KEY}")
        self.assertEqual(output.getvalue(), "")

    def test_setup_creates_loadable_env_and_preserves_edits(self) -> None:
        (self.root / ".env.example").write_text("OPENAI_MODEL=initial\n", encoding="utf-8")
        with patch("setup.ROOT_DIR", self.root), \
                patch("setup.FRONTEND_DIR", self.root / "frontend"), \
                patch.dict(os.environ, {"VIRTUAL_ENV": "active"}), \
                patch("setup.run_cmd", return_value=0), redirect_stdout(io.StringIO()):
            self.assertEqual(setup.main(), 0)
            self.write_env("OPENAI_MODEL=edited\n")
            self.assertEqual(setup.main(), 0)
        runtime_environment.load_environment()
        self.assertEqual(os.environ["OPENAI_MODEL"], "edited")

    def test_web_launcher_reads_settings_and_children_inherit_provider_config(self) -> None:
        self.write_env(f"OPENAI_API_KEY={SECRET}\nHOST=127.0.0.2\nPORT=8300\nFRONTEND_PORT=5300\n")
        os.environ["PORT"] = "8400"
        frontend = self.root / "frontend"
        frontend.mkdir()
        (frontend / "package.json").write_text("{}", encoding="utf-8")
        launched = []

        def launch(command, **kwargs):
            launched.append((command, kwargs, dict(os.environ)))
            return Mock(poll=Mock(return_value=0), returncode=0)

        output = io.StringIO()
        with patch("run.get_venv_python", return_value=sys.executable), \
                patch("run.FRONTEND_DIR", frontend), \
                patch("run.available_port", side_effect=lambda start, *args: start), \
                patch("run.subprocess.Popen", side_effect=launch), \
                patch("run.time.sleep"), patch("run.signal.signal"), \
                redirect_stdout(output):
            self.assertEqual(run.main(), 0)
        backend, frontend_process = launched
        self.assertEqual(backend[0][backend[0].index("--port") + 1], "8400")
        self.assertEqual(backend[0][backend[0].index("--host") + 1], "127.0.0.2")
        self.assertEqual(backend[2]["OPENAI_API_KEY"], SECRET)
        self.assertEqual(frontend_process[1]["env"]["VITE_BACKEND_URL"], "http://127.0.0.2:8400")
        self.assertEqual(frontend_process[0][frontend_process[0].index("--port") + 1], "5300")
        self.assertNotIn(SECRET, output.getvalue())
        self.assertNotIn(SECRET, json.dumps([process[0] for process in launched]))

    def test_web_launcher_selects_venv_before_importing_dotenv(self) -> None:
        class HandedOff(Exception):
            pass

        selected = str(self.root / ".venv" / "bin" / "python")
        with patch("run.get_venv_python", return_value=selected), \
                patch("run.os.execv", side_effect=HandedOff) as handoff, \
                patch("run.load_environment") as load:
            with self.assertRaises(HandedOff):
                run.main()
        self.assertEqual(handoff.call_args.args[0], selected)
        self.assertEqual(handoff.call_args.args[1][:2], [selected, str(ROOT / "run.py")])
        load.assert_not_called()

    def test_invalid_port_error_does_not_echo_value_or_chained_exception(self) -> None:
        os.environ["PORT"] = SECRET
        try:
            run.requested_port("PORT", 8000)
        except RuntimeError as error:
            rendered = "".join(traceback.format_exception(error))
        else:
            self.fail("Invalid port was accepted")
        self.assertIn("PORT must be an integer", rendered)
        self.assertNotIn(SECRET, rendered)

    def test_orchestrator_and_direct_cli_configure_connector_before_generation(self) -> None:
        source = self.root / "source.txt"
        source.write_text("Source text", encoding="utf-8")
        output = self.root / "output.txt"
        self.write_env(f"OPENAI_API_KEY={SECRET}\nOPENAI_MODEL=env-model\n")
        arguments = ["--connector", "openai", "--input", str(source), "--output", str(output)]
        for entrypoint, extra in (
            (orchestrator_main, ["--action", "create_reports"]),
            (lambda argv: run_action_cli("create_reports", argv), []),
        ):
            with self.subTest(entrypoint=entrypoint), patch.dict(os.environ, {}, clear=True), \
                    patch("cli_runtime.run_pipeline", return_value=PipelineResult("generated", {}, {}, self.root)) as pipeline, \
                    self.assertLogs("content_generator", level="INFO") as logs, \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(entrypoint(arguments + extra), 0)
                connector = pipeline.call_args.kwargs["connector"]
                self.assertEqual(connector.api_key, SECRET)
                self.assertEqual(connector.model, "env-model")
                self.assertEqual(output.read_text(encoding="utf-8"), "generated")
                self.assertNotIn(SECRET, "\n".join(logs.output))

    def test_study_set_prompt_loads_project_env_after_changing_directory(self) -> None:
        self.write_env(f"OPENAI_API_KEY={SECRET}\nOPENAI_MODEL=study-model\n")
        work = self.root / "study"
        work.mkdir()
        (work / "source.txt").write_text("Source text", encoding="utf-8")
        (work / "study-set-config.json").write_text(json.dumps({
            "connector": "openai",
            "model": "explicit-study-model",
            "files": [{"input": ".", "output": "output", "inputPattern": "*.txt", "types": ["report"]}],
        }), encoding="utf-8")
        commands = iter([f'cd "{work}"', "generate", "exit"])
        with chdir(self.root), patch("study_set._command_reader", return_value=lambda _: next(commands)), \
                patch("cli_runtime.run_pipeline", return_value=PipelineResult("generated", {}, {}, work)) as pipeline, \
                self.assertLogs("content_generator", level="INFO") as logs, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(study_set.main([]), 0)
        connector = pipeline.call_args.kwargs["connector"]
        self.assertEqual(connector.api_key, SECRET)
        self.assertEqual(connector.model, "explicit-study-model")
        self.assertNotIn(SECRET, "\n".join(logs.output))

    def test_batch_loads_env_before_resolving_yaml_placeholders(self) -> None:
        self.write_env(f"OPENAI_API_KEY={SECRET}\n")
        source_dir = self.root / "input"
        source_dir.mkdir()
        (source_dir / "source.txt").write_text("Source text", encoding="utf-8")
        config = self.root / "config.yaml"
        config.write_text("provider:\n  default: openai\nopenai:\n  api_key: '${OPENAI_API_KEY}'\n", encoding="utf-8")
        with patch("main.subprocess.run", return_value=SimpleNamespace(returncode=0)) as child:
            code = batch_main([
                "--config", str(config), "--input-dir", str(source_dir),
                "--output-dir", str(self.root / "output"),
                "--checkpoint", str(self.root / "checkpoint.json"),
                "--actions", "create_reports",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(child.call_args.kwargs["env"]["OPENAI_API_KEY"], SECRET)
        self.assertNotIn(SECRET, json.dumps(child.call_args.args[0]))

    def test_experiment_loads_environment(self) -> None:
        self.write_env("OPENAI_MODEL=experiment-model\n")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(experiment_main(["--list"]), 0)
        self.assertEqual(os.environ["OPENAI_MODEL"], "experiment-model")

    def test_direct_backend_launch_loads_before_metadata_without_exposing_key(self) -> None:
        self.write_env(f"OPENAI_API_KEY={SECRET}\nOPENAI_MODEL=backend-model\nHOST=127.0.0.2\nPORT=8300\n")
        script = """
import json, os, runpy, sys, types
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import runtime_environment
runtime_environment.ENV_FILE = Path(sys.argv[2])
os.environ['OPENAI_MODEL'] = 'shell-model'
def serve(app, **kwargs):
    assert app == 'server:app'
    assert kwargs['host'] == '127.0.0.2'
    assert kwargs['port'] == 8300
sys.modules['uvicorn'] = types.SimpleNamespace(run=serve)
module = runpy.run_module('server', run_name='__main__')
config = module['get_config']()
assert config['connectors']['openai']['env_configured']
assert config['connectors']['openai']['default_model'] == 'shell-model'
assert os.environ['OPENAI_API_KEY'] not in json.dumps(config)
"""
        completed = subprocess.run(
            [sys.executable, "-c", script, str(ROOT), str(self.env_file)],
            cwd=self.root, capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn(SECRET, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
