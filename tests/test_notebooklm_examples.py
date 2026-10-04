from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from notebooklm_converter import artifact_from_metadata, convert_notebooklm_artifact
from notebooklm_renderers import render_artifact
from tests.test_notebooklm import exported_artifact, quiz_payload


EXAMPLES = Path(__file__).resolve().parents[1] / "output-example"


def example_json(folder: str, name: str) -> dict:
    return json.loads((EXAMPLES / folder / name).read_text(encoding="utf-8"))


class NotebookLMRendererContractTests(unittest.TestCase):
    """Small checked-in contracts keep coverage independent of ignored examples."""

    def test_flashcard_header_order_and_actual_tab_separator(self) -> None:
        payload = {"title": "Deck", "description": "Two cards", "cards": [
            {"type": "basic", "front": "First question", "back": "First answer", "tags": ["topic"], "source_ids": ["source.txt"]},
            {"type": "basic", "front": "Second question", "back": "Second answer", "tags": ["topic"], "source_ids": ["source.txt"]},
        ]}
        self.assertEqual(render_artifact(payload, "create_flashcards", ".txt"),
                         "# Deck\n# Two cards\nFirst question\tFirst answer\nSecond question\tSecond answer\n")

    def test_datatable_csv_declared_fields_then_source_and_page(self) -> None:
        payload = {"title": "Terms", "fields": [
            {"name": "term", "description": "Term", "example": "A"},
            {"name": "definition", "description": "Meaning", "example": "Comma, value"},
        ], "data": [{"term": "A", "definition": "Comma, value", "source_id": "source.txt", "page": "N/A"}]}
        self.assertEqual(render_artifact(payload, "create_datatables", ".csv"),
                         'term,definition,source_id,page\nA,"Comma, value",source.txt,N/A\n')

    def test_mindmap_heading_depth_and_unique_mermaid_node_ids(self) -> None:
        tree = {"name": "Root", "children": [
            {"name": "Branch", "children": [{"name": "Leaf", "children": []}]},
            {"name": "Branch", "children": []},
        ]}
        self.assertEqual(render_artifact(tree, "create_mindmaps", ".md"), "# Root\n## Branch\n### Leaf\n## Branch\n")
        self.assertEqual(render_artifact(tree, "create_mindmaps", ".mmd"),
                         'mindmap\n  root(("Root"))\n  n1["Branch"]\n    n2["Leaf"]\n  n3["Branch"]\n')

    def test_slides_markdown_sections_notes_and_sources(self) -> None:
        payload = {"title": "Deck", "slides": [{"title": "Overview", "subtitle": "Lesson",
                    "bullets": ["First", "Second", "Third"], "speaker_notes": "Notes",
                    "image_query": "diagram", "source_ids": ["source.txt"]}]}
        self.assertEqual(render_artifact(payload, "create_slides", ".md"),
                         "# Deck\n\n---\n## Overview\n### Lesson\n\n- First\n- Second\n- Third\n\n**Speaker Notes:** Notes\n\n**Sources:** source.txt\n")

    def test_podcast_markdown_uses_observed_cast_names_and_scene_order(self) -> None:
        payload = {"episode_title": "Episode", "podcast_show": "Show", "cast": [
            {"speaker_id": "one", "name": "Speaker one"}, {"speaker_id": "two", "name": "Speaker two"}],
            "script": [{"segment_name": "Start", "scenes": [
                {"speaker_id": "one", "directions": "[calm]", "dialogue": "Hello"},
                {"speaker_id": "two", "dialogue": "Hi"}]}]}
        self.assertEqual(render_artifact(payload, "create_podcasts", ".md"),
                         "# Episode\n_Show_\n\n## Start\n\n**Speaker one:** Hello\n\n**Speaker two:** Hi\n")

    def test_converted_quiz_keys_match_example_contract_without_extra_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata, _ = exported_artifact(root, "Quiz", "APP_TYPE_QUIZ", quiz_payload())
            output = root / "quiz.json"
            convert_notebooklm_artifact(artifact_from_metadata(metadata), output)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(list(payload), ["title", "description", "questions"])
            self.assertEqual(list(payload["questions"][0]), ["question", "options", "correct", "explanation", "difficulty", "sources"])
            self.assertTrue(output.with_suffix(".metadata.json").is_file())

    def test_report_and_qanda_structured_exports_preserve_canonical_content(self) -> None:
        report = {"title": "Report", "executive_summary": "Observed summary", "sections": [
            {"title": "Finding", "content": "Observed detail", "claims": [
                {"claim": "Recorded claim", "citations": [{"source_id": "source.txt", "page": None, "chunk_id": None}]}]}],
            "conclusions": "Recorded conclusion"}
        qanda = {"title": "Exercise", "description": "Recorded exercise", "questions": [
            {"id": "exercise-one", "kind": "source_exercise", "exercise_label": "1", "question": "Question?",
             "answer": "Recorded answer", "placeholder": "Answer here", "required": True, "source_ids": ["source.txt"]}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, kind, payload in (("Report", "ARTIFACT_TYPE_REPORT", report), ("Exercise", "APP_TYPE_QANDA", qanda)):
                with self.subTest(kind=kind):
                    metadata, _ = exported_artifact(root, name, kind, json.dumps(payload), extension=".content.json")
                    output = root / "output" / (name + ".json")
                    convert_notebooklm_artifact(artifact_from_metadata(metadata), output)
                    self.assertEqual(json.loads(output.read_text(encoding="utf-8")), payload)
        markdown = render_artifact(report, "create_reports", ".md")
        self.assertEqual(markdown, "# Report\n\n## Executive Summary\nObserved summary\n\n## Finding\nObserved detail\n\n"
                         "### Key Claims\n- Recorded claim\n  Sources: [source.txt]\n\n## Conclusions\nRecorded conclusion\n")

    def test_infographic_formats_preserve_text_and_sanitize_inline_svg(self) -> None:
        payload = {"title": "Diagram <title>", "subtitle": "Observed caption", "sections": [
            {"type": "quote", "title": "Text", "value": "Recorded words", "label": None, "items": [], "panel": None, "span": "full"},
            {"type": "svg", "title": "Diagram", "value": "assets/diagram.svg", "label": "Observed image", "items": [], "panel": None, "span": "full"}],
            "assets": [{"id": "assets/diagram.svg", "svg": '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="80">'
                       '<script>active_content()</script><rect width="100" height="80" fill="blue" onclick="active_content()"/></svg>'}]}
        markdown = render_artifact(payload, "create_infographics", ".md")
        self.assertIn("> Recorded words", markdown)
        self.assertIn("![Observed image](assets/diagram.svg)", markdown)
        html = render_artifact(payload, "create_infographics", ".html")
        self.assertIn("Diagram &lt;title&gt;", html)
        self.assertIn("Recorded words", html)
        self.assertNotIn("active_content", html)
        self.assertIn("Recorded words", render_artifact(payload, "create_infographics", ".wireframe.txt"))
        self.assertIn("Recorded words", render_artifact(payload, "create_infographics", ".svg"))


@unittest.skipUnless(EXAMPLES.is_dir(), "Local output-example reference files are not included in the repository")
class NotebookLMRendererExampleTests(unittest.TestCase):
    def assert_example_render(self, folder: str, name: str, action: str, extension: str) -> None:
        payload = example_json(folder, name + ".json")
        expected = (EXAMPLES / folder / (name + extension)).read_text(encoding="utf-8")
        self.assertEqual(render_artifact(payload, action, extension), expected)

    def test_flashcard_txt_matches_complete_example(self) -> None:
        self.assert_example_render("flashcards", "flashcards_20260908194812", "create_flashcards", ".txt")

    def test_datatable_csv_matches_complete_example(self) -> None:
        self.assert_example_render("datatables", "20260908194812", "create_datatables", ".csv")

    def test_mindmap_markdown_matches_complete_example(self) -> None:
        self.assert_example_render("mindmaps", "mindmap_20260908194812", "create_mindmaps", ".md")

    def test_mindmap_mermaid_matches_complete_example(self) -> None:
        self.assert_example_render("mindmaps", "mindmap_20260908194812", "create_mindmaps", ".mmd")

    def test_slides_markdown_matches_complete_example(self) -> None:
        self.assert_example_render("slides", "slides_20260908194812", "create_slides", ".md")

    def test_podcast_markdown_matches_both_complete_examples(self) -> None:
        for episode in (0, 1):
            with self.subTest(episode=episode):
                self.assert_example_render("podcasts", f"20260908194812_episode{episode}", "create_podcasts", ".md")

    def test_report_markdown_matches_complete_example(self) -> None:
        self.assert_example_render("reports", "report_20260908194812", "create_reports", ".md")

    def test_infographic_markdown_matches_complete_example(self) -> None:
        self.assert_example_render("infographics", "infographic_20260908194812", "create_infographics", ".md")

    def test_rendering_keeps_input_unchanged(self) -> None:
        payload = example_json("mindmaps", "mindmap_20260908194812.json")
        original = copy.deepcopy(payload)
        render_artifact(payload, "create_mindmaps", ".mmd")
        self.assertEqual(payload, original)


@unittest.skipUnless(EXAMPLES.is_dir(), "Local output-example reference files are not included in the repository")
class NotebookLMCanonicalExampleTests(unittest.TestCase):
    def test_converted_mindmap_matches_example_json_and_both_text_formats(self) -> None:
        expected = example_json("mindmaps", "mindmap_20260908194812.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata, _ = exported_artifact(root, "Example Tree", "APP_TYPE_MINDMAP", expected)
            artifact = artifact_from_metadata(metadata)
            output = root / "output" / "mindmap.json"
            convert_notebooklm_artifact(artifact, output)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), expected)
            for extension in (".md", ".mmd"):
                with self.subTest(extension=extension):
                    derived = output.with_suffix(extension)
                    convert_notebooklm_artifact(artifact, derived)
                    example = EXAMPLES / "mindmaps" / ("mindmap_20260908194812" + extension)
                    self.assertEqual(derived.read_text(encoding="utf-8"), example.read_text(encoding="utf-8"))

    def test_quiz_json_has_exact_example_keys_without_conversion_fields(self) -> None:
        example = example_json("quizzes", "quiz_20260908194812.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata, _ = exported_artifact(root, "Quiz", "APP_TYPE_QUIZ", quiz_payload())
            output = root / "quiz.json"
            convert_notebooklm_artifact(artifact_from_metadata(metadata), output)
            actual = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(list(actual), list(example))
            self.assertEqual(list(actual["questions"][0]), list(example["questions"][0]))
            self.assertEqual(actual["questions"][0]["correct"], 1)
            self.assertEqual(actual["questions"][0]["options"],
                             [option["text"] for option in quiz_payload()["quiz"][0]["answerOptions"]])
            self.assertTrue(output.with_suffix(".metadata.json").is_file())

    def test_flashcard_json_has_exact_example_keys_and_preserves_card_order(self) -> None:
        example = example_json("flashcards", "flashcards_20260908194812.json")
        cards = [{"f": {"flashcardContentBlock": [{"type": "text", "content": card["front"]}]},
                  "b": {"flashcardContentBlock": [{"type": "text", "content": card["back"]}]}, "c": 1}
                 for card in example["cards"][:3]]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata, _ = exported_artifact(root, "Cards", "APP_TYPE_FLASHCARDS", {"flashcards": cards})
            output = root / "cards.json"
            convert_notebooklm_artifact(artifact_from_metadata(metadata), output)
            actual = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(list(actual), list(example))
            self.assertEqual(list(actual["cards"][0]), list(example["cards"][0]))
            self.assertEqual([card["front"] for card in actual["cards"]],
                             [card["front"] for card in example["cards"][:3]])
            self.assertEqual([card["back"] for card in actual["cards"]],
                             [card["back"] for card in example["cards"][:3]])

    def test_datatable_json_and_csv_use_example_field_and_source_column_order(self) -> None:
        example = example_json("datatables", "20260908194812.json")
        names = [field["name"] for field in example["fields"][:3]]
        rows = [{name: row[name] for name in names} for row in example["data"][:2]]
        source_name = example["data"][0]["source_id"]
        table = "| " + " | ".join(names) + " |\n| " + " | ".join("---" for _ in names) + " |\n"
        table += "".join("| " + " | ".join(row[name] for name in names) + " |\n" for row in rows)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata, _ = exported_artifact(root, "Table", "ARTIFACT_TYPE_TABLE", table, extension=".md")
            artifact = artifact_from_metadata(metadata, source_names=[source_name])
            output = root / "table.json"
            convert_notebooklm_artifact(artifact, output)
            actual = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(list(actual), list(example))
            self.assertEqual(list(actual["fields"][0]), list(example["fields"][0]))
            self.assertEqual(list(actual["data"][0]), [*names, "source_id", "page"])
            self.assertEqual(actual["data"], [{**row, "source_id": source_name, "page": "N/A"} for row in rows])
            output_csv = output.with_suffix(".csv")
            convert_notebooklm_artifact(artifact, output_csv)
            self.assertEqual(output_csv.read_text(encoding="utf-8").splitlines()[0], ",".join([*names, "source_id", "page"]))


if __name__ == "__main__":
    unittest.main()
