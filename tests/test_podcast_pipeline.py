from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cli_runtime import execute_generation
from connectors.base import GenerationResponse
from errors import ProviderError
from pipeline.engine import PipelineOptions
from podcast_policy import podcast_runtime


SOURCE = (
    "Alpha source explains its central concept.\n\n"
    "Beta source offers a representative example.\n"
)
ASSEMBLY_REQUEST = "Assemble the source-grounded drafts"


def episode(words: int, concepts: tuple[str, ...]) -> dict:
    body = " ".join(concepts[index % len(concepts)] for index in range(words - 2))
    return {
        "episode_title": "Study overview",
        "podcast_show": "Study together",
        "cast": [
            {"speaker_id": "maya", "host_id": "HOST_A", "name": "Maya",
             "voice_file": "af_heart", "style": "Calm"},
            {"speaker_id": "leo", "host_id": "HOST_B", "name": "Leo",
             "voice_file": "am_michael", "style": "Curious"},
        ],
        "script": [
            {"segment_name": "Introduction", "scenes": [
                {"speaker_id": "maya", "dialogue": "Welcome."},
            ]},
            {"segment_name": "Discussion", "scenes": [
                {"speaker_id": "leo", "dialogue": body,
                 "directions": "[calm] [pause=1000] [pause=2000]"},
            ]},
            {"segment_name": "Sign-off", "scenes": [
                {"speaker_id": "maya", "dialogue": "Goodbye."},
            ]},
        ],
    }


def transcript(value: dict) -> str:
    # Long metadata makes counting the complete Markdown rather than speech fail.
    lines = ["# Study overview " + "metadata " * 100, "_Study together_", ""]
    for segment in value["script"]:
        lines.append("## " + segment["segment_name"])
        for scene in segment["scenes"]:
            lines.append(
                f"**{scene['speaker_id'].title()}:** "
                f"{scene.get('directions', '')} {scene['dialogue']}"
            )
        lines.append("")
    return "\n".join(lines)


class RecordingPodcastConnector:
    model = "fake-podcast-model"

    def __init__(self, *, markdown: bool = False, draft_words: int = 1800,
                 assembly_words: tuple[int, ...] = (2600,)) -> None:
        self.markdown = markdown
        self.draft_words = draft_words
        self.assembly_words = assembly_words
        self.assembly_calls = 0
        self.calls: list[dict] = []
        self.drafts: list[dict] = []

    def generate_response(self, **request) -> GenerationResponse:
        self.calls.append(request)
        if ASSEMBLY_REQUEST in request["user_prompt"]:
            words = self.assembly_words[min(self.assembly_calls, len(self.assembly_words) - 1)]
            self.assembly_calls += 1
            value = episode(words, ("alpha", "beta"))
        else:
            concept = "alpha" if not self.drafts else "beta"
            value = episode(self.draft_words, (concept,))
            self.drafts.append(value)
        text = transcript(value) if self.markdown else json.dumps(value)
        return GenerationResponse(text, finish_reason="stop", input_tokens=10, output_tokens=20)


def options(work_dir: Path, *, aggregation: str = "hierarchical",
            strategy: str = "chunked", retries: int = 0) -> PipelineOptions:
    return PipelineOptions(
        strategy=strategy,
        chunk_characters=64,
        aggregation=aggregation,
        generation_passes=1,
        retries=retries,
        work_dir=work_dir,
        profile_overrides={
            "context_window": 128000,
            "max_output_tokens": 12000,
            "reserved_output_tokens": 12000,
        },
    )


class PodcastPipelineTests(unittest.TestCase):
    def test_chunked_json_assembles_one_bounded_episode_and_resumes(self) -> None:
        for aggregation in ("deterministic", "hierarchical"):
            with self.subTest(aggregation=aggregation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "chapter.txt"
                source.write_text(SOURCE, encoding="utf-8")
                output = root / "caller-selected.json"
                pipeline_options = options(root / "work", aggregation=aggregation)
                connector = RecordingPodcastConnector()
                with patch("cli_runtime.create_connector", return_value=connector):
                    result = execute_generation(
                        connector_name="ollama", input_path=source,
                        action="create_podcasts", output_path=output,
                        options=pipeline_options,
                    )

                self.assertEqual(result.metrics["chunk_count"], 2)
                self.assertEqual(len(connector.calls), 3)
                self.assertEqual(len(connector.drafts), 2)
                draft_runtimes = [podcast_runtime(value) for value in connector.drafts]
                self.assertTrue(all(value["estimated_duration_minutes"] <= 20 for value in draft_runtimes))
                self.assertGreater(sum(value["estimated_duration_minutes"] for value in draft_runtimes), 20)
                assembly_calls = [request for request in connector.calls if ASSEMBLY_REQUEST in request["user_prompt"]]
                self.assertEqual(len(assembly_calls), 1)
                self.assertIn("alpha alpha", assembly_calls[0]["user_prompt"])
                self.assertIn("beta beta", assembly_calls[0]["user_prompt"])

                final = json.loads(output.read_text(encoding="utf-8"))
                self.assertIsInstance(final, dict)
                self.assertNotIn("episodes", final)
                self.assertEqual(final, json.loads(result.text))
                self.assertEqual(list(root.glob("*.json")), [output])
                spoken = " ".join(scene["dialogue"] for segment in final["script"] for scene in segment["scenes"])
                self.assertEqual(spoken.count("Welcome."), 1)
                self.assertEqual(spoken.count("Goodbye."), 1)
                self.assertIn("alpha", spoken)
                self.assertIn("beta", spoken)
                self.assertTrue(result.validation["valid"], result.validation)
                runtime = result.validation["podcast_runtime"]
                self.assertEqual(runtime["dialogue_words"], 2600)
                self.assertEqual(runtime["pause_milliseconds"], 3000)
                self.assertAlmostEqual(runtime["estimated_duration_minutes"], 2600 / 150 + 3000 / 60000)
                self.assertLessEqual(runtime["estimated_duration_minutes"], 20)

                checkpoint = root / "work" / "aggregate" / "podcast_final.checkpoint.json"
                self.assertTrue(checkpoint.is_file())
                resumed_connector = RecordingPodcastConnector()
                with patch("cli_runtime.create_connector", return_value=resumed_connector):
                    resumed = execute_generation(
                        connector_name="ollama", input_path=source,
                        action="create_podcasts", output_path=output,
                        options=pipeline_options,
                    )
                self.assertEqual(resumed_connector.calls, [])
                self.assertEqual(resumed.text, result.text)
                self.assertEqual(resumed.validation["podcast_runtime"], runtime)

    def test_chunked_markdown_assembles_one_episode_and_counts_spoken_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chapter.txt"
            source.write_text(SOURCE, encoding="utf-8")
            output = root / "caller-selected.md"
            connector = RecordingPodcastConnector(markdown=True)
            with patch("cli_runtime.create_connector", return_value=connector):
                result = execute_generation(
                    connector_name="ollama", input_path=source,
                    action="create_podcasts", output_path=output,
                    options=options(root / "work"),
                )

            self.assertEqual(result.metrics["chunk_count"], 2)
            self.assertEqual(len(connector.calls), 3)
            assembly_calls = [request for request in connector.calls if ASSEMBLY_REQUEST in request["user_prompt"]]
            self.assertEqual(len(assembly_calls), 1)
            self.assertFalse(assembly_calls[0]["json_mode"])
            text = output.read_text(encoding="utf-8")
            self.assertEqual(text, result.text)
            self.assertEqual(list(root.glob("*.md")), [output])
            self.assertEqual(text.count("Welcome."), 1)
            self.assertEqual(text.count("Goodbye."), 1)
            self.assertIn("alpha beta", text)
            self.assertTrue(result.validation["valid"], result.validation)
            runtime = result.validation["podcast_runtime"]
            self.assertEqual(runtime["dialogue_words"], 2600)
            self.assertGreater(len(text.split()), runtime["dialogue_words"])
            self.assertEqual(runtime["pause_milliseconds"], 3000)
            self.assertAlmostEqual(runtime["estimated_duration_minutes"], 2600 / 150 + 3000 / 60000)
            self.assertLessEqual(runtime["estimated_duration_minutes"], 20)

    def test_baseline_runtime_failure_does_not_write_requested_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chapter.txt"
            source.write_text(SOURCE, encoding="utf-8")
            output = root / "requested" / "podcast.json"
            connector = RecordingPodcastConnector(draft_words=3001)
            with patch("cli_runtime.create_connector", return_value=connector):
                with self.assertRaisesRegex(ProviderError, "maximum is 20 minutes"):
                    execute_generation(
                        connector_name="ollama", input_path=source,
                        action="create_podcasts", output_path=output,
                        options=options(root / "work", strategy="baseline"),
                    )
            self.assertEqual(len(connector.calls), 1)
            self.assertFalse(output.exists())
            self.assertFalse(output.parent.exists())

    def test_overlong_assembly_is_repaired_into_one_valid_episode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chapter.txt"
            source.write_text(SOURCE, encoding="utf-8")
            output = root / "podcast.json"
            connector = RecordingPodcastConnector(assembly_words=(3100, 2600))
            with patch("cli_runtime.create_connector", return_value=connector):
                result = execute_generation(
                    connector_name="ollama", input_path=source,
                    action="create_podcasts", output_path=output,
                    options=options(root / "work", retries=1),
                )
            self.assertEqual(len(connector.calls), 4)
            self.assertEqual(connector.assembly_calls, 2)
            self.assertIn("Fix these validation failures", connector.calls[-1]["user_prompt"])
            self.assertIn("maximum is 20 minutes", connector.calls[-1]["user_prompt"])
            self.assertEqual(result.metrics["repair_count"], 1)
            self.assertTrue(result.validation["valid"], result.validation)
            self.assertEqual(result.validation["podcast_runtime"]["dialogue_words"], 2600)
            self.assertLessEqual(result.validation["podcast_runtime"]["estimated_duration_minutes"], 20)
            final = json.loads(output.read_text(encoding="utf-8"))
            self.assertIsInstance(final, dict)
            self.assertEqual(list(root.glob("*.json")), [output])

    def test_default_local_model_profile_assembles_short_multichunk_episode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chapter.txt"
            source.write_text(SOURCE, encoding="utf-8")
            output = root / "podcast.json"
            connector = RecordingPodcastConnector(draft_words=100, assembly_words=(100,))
            with patch("cli_runtime.create_connector", return_value=connector):
                result = execute_generation(
                    connector_name="the_connector", input_path=source,
                    action="create_podcasts", output_path=output,
                    options=PipelineOptions(
                        strategy="chunked", chunk_characters=64,
                        generation_passes=1, retries=0, work_dir=root / "work",
                    ),
                )
            self.assertEqual(result.metrics["chunk_count"], 2)
            self.assertEqual(len(connector.calls), 3)
            self.assertEqual(connector.assembly_calls, 1)
            self.assertTrue(result.validation["valid"], result.validation)
            self.assertEqual(result.validation["podcast_runtime"]["dialogue_words"], 100)
            self.assertLessEqual(result.validation["podcast_runtime"]["estimated_duration_minutes"], 20)
            self.assertIsInstance(json.loads(output.read_text(encoding="utf-8")), dict)

    def test_repeatedly_overlong_assembly_exhausts_retries_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chapter.txt"
            source.write_text(SOURCE, encoding="utf-8")
            output = root / "podcast.json"
            connector = RecordingPodcastConnector(assembly_words=(3100,))
            with patch("cli_runtime.create_connector", return_value=connector):
                with self.assertRaisesRegex(ProviderError, "Podcast assembly failed validation"):
                    execute_generation(
                        connector_name="ollama", input_path=source,
                        action="create_podcasts", output_path=output,
                        options=options(root / "work", retries=1),
                    )
            self.assertEqual(len(connector.calls), 4)
            self.assertEqual(connector.assembly_calls, 2)
            self.assertFalse(output.exists())
            self.assertFalse((root / "work" / "aggregate" / "podcast_final.checkpoint.json").exists())


if __name__ == "__main__":
    unittest.main()
