from __future__ import annotations

import json
import unittest
from pathlib import Path

from action_base import build_generation_prompts
from create_podcasts import generate
from errors import ProviderError
from pipeline.validator import editorial_warnings, validate_artifact, validate_output
from podcast_policy import podcast_errors, podcast_runtime


def podcast(words: int = 10, *, pauses: str = "", title: str = "Study overview") -> dict:
    return {
        "episode_title": title,
        "podcast_show": "Study together",
        "cast": [
            {"speaker_id": "maya", "host_id": "HOST_A", "name": "Maya", "voice_file": "af_heart", "style": "Calm"},
            {"speaker_id": "leo", "host_id": "HOST_B", "name": "Leo", "voice_file": "am_michael", "style": "Curious"},
        ],
        "script": [{"segment_name": "Review", "scenes": [
            {"speaker_id": "maya", "dialogue": " ".join(["idea"] * words), "directions": pauses},
        ]}],
    }


class PodcastConnector:
    def __init__(self, result: str):
        self.result = result
        self.calls = []

    def generate(self, **request):
        self.calls.append(request)
        return self.result


class PodcastPolicyTests(unittest.TestCase):
    def test_exact_twenty_minute_boundary_without_pauses(self) -> None:
        self.assertEqual(podcast_errors(podcast(3000)), [])
        self.assertEqual(podcast_runtime(podcast(3000))["estimated_duration_minutes"], 20)
        self.assertTrue(podcast_errors(podcast(3001)))
        self.assertTrue(validate_output(json.dumps(podcast(3000)), "create_podcasts")["valid"])
        self.assertFalse(validate_output(json.dumps(podcast(3001)), "create_podcasts")["valid"])

    def test_pauses_reduce_dialogue_allowance_at_exact_boundary(self) -> None:
        script = podcast(2999, pauses="[calm] [pause=100] [pause=300]")
        self.assertEqual(podcast_runtime(script), {
            "dialogue_words": 2999,
            "pause_milliseconds": 400,
            "estimated_duration_minutes": 20,
        })
        self.assertEqual(validate_artifact(script, "create_podcasts"), [])
        script["script"][0]["scenes"][0]["directions"] += " [pause=1]"
        self.assertTrue(podcast_errors(script))

    def test_counts_only_dialogue_and_scene_directions(self) -> None:
        script = podcast(10, pauses="[pause=200]")
        script["episode_title"] = "metadata " * 4000
        script["cast"][0]["style"] = "metadata " * 4000
        script["metadata"] = {"text": "metadata " * 4000, "directions": "[pause=1200001]"}
        script["script"][0]["segment_name"] = "metadata " * 4000
        runtime = podcast_runtime(script)
        self.assertEqual(runtime["dialogue_words"], 10)
        self.assertEqual(runtime["pause_milliseconds"], 200)
        self.assertEqual(podcast_errors(script), [])

    def test_short_podcast_has_no_minimum_warning(self) -> None:
        self.assertEqual(podcast_errors(podcast(1)), [])
        self.assertEqual(editorial_warnings(podcast(1), "create_podcasts"), [])

    def test_episode_series_wrappers_and_titles_are_rejected(self) -> None:
        for value in ([podcast(), podcast()], {"episodes": [podcast()]}, podcast(title="Topic — Episode 1 of 2")):
            with self.subTest(value=type(value).__name__):
                self.assertTrue(podcast_errors(value))
                self.assertFalse(validate_output(json.dumps(value), "create_podcasts")["valid"])
        self.assertTrue(podcast_errors("# Topic — Episode 1 of 2\n**Maya:** Hello."))
        script = podcast()
        script["script"][0]["scenes"][0]["dialogue"] = "The author calls the example Episode 1 of 2."
        self.assertEqual(podcast_errors(script), [])
        self.assertEqual(podcast_errors("# Topic\n**Maya:** They called it Episode 1 of 2."), [])

    def test_markdown_counts_labelled_dialogue_continuations_and_pauses(self) -> None:
        text = "# Study overview\n_Study together_\n\n## Review\n**Maya:** First idea. [calm] [pause=400]\nAnother idea.\n**Leo:** Final idea."
        self.assertEqual(podcast_runtime(text), {
            "dialogue_words": 6,
            "pause_milliseconds": 400,
            "estimated_duration_minutes": 2800 / 60_000,
        })
        self.assertTrue(validate_output(text, "create_podcasts", ".md")["valid"])

    def test_transcript_formatting_does_not_evade_cap(self) -> None:
        for text in ("**Maya:** " + "idea " * 3001, "Maya: " + "idea " * 3001, "idea " * 3001):
            with self.subTest(prefix=text[:10]):
                self.assertEqual(podcast_runtime(text)["dialogue_words"], 3001)
                self.assertFalse(validate_output(text, "create_podcasts", ".md")["valid"])

    def test_late_italicized_dialogue_is_counted(self) -> None:
        text = "# Study overview\n_Study together_\n\n## Discussion\n_" + " ".join(["idea"] * 3001) + "_"
        self.assertEqual(podcast_runtime(text)["dialogue_words"], 3001)
        self.assertFalse(validate_output(text, "create_podcasts", ".md")["valid"])

    def test_custom_skill_prompt_retains_authoritative_limit(self) -> None:
        system, _ = build_generation_prompts(
            action="create_podcasts", artifact_name="podcast", source_text="A source.",
            source_path=Path("chapter.txt"), skill_text="Split into 9 episodes with at least 4500 words.",
        )
        self.assertIn("at most 20 minutes", system)
        self.assertIn("exactly one complete podcast episode", system)
        self.assertIn("no minimum runtime", system)

    def test_direct_wrapper_validates_canonical_json_and_markdown(self) -> None:
        for extension, text in ((".json", json.dumps(podcast(1))), (".md", "# Overview\n**Maya:** A short review.")):
            with self.subTest(extension=extension):
                connector = PodcastConnector(text)
                result = generate("Source", Path("chapter.txt"), "Custom skill", connector, Path("selected" + extension))
                self.assertTrue(validate_output(result, "create_podcasts", extension)["valid"])
                self.assertIn("selected" + extension, connector.calls[0]["user_prompt"])

    def test_direct_wrapper_rejects_overlong_or_invalid_output(self) -> None:
        for extension, text in ((".json", json.dumps(podcast(3001))), (".json", "{}"), (".md", "**Maya:** " + "idea " * 3001)):
            with self.subTest(extension=extension, length=len(text)):
                with self.assertRaises(ProviderError):
                    generate("Source", Path("chapter.txt"), "Custom skill", PodcastConnector(text), Path("selected" + extension))


if __name__ == "__main__":
    unittest.main()
